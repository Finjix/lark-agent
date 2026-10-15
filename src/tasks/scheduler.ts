import PQueue from 'p-queue';
import {config} from '../config.js';
import {TaskService} from './service.js';
import type {Task} from '../types.js';
import {log,safeError} from '../log.js';
export function canSchedule(task:Task,active:Task[],globalLimit:number,userLimit:number){
  if(active.length>=globalLimit)return false;
  if(active.filter(t=>t.tenant_key===task.tenant_key&&t.user_id===task.user_id).length>=userLimit)return false;
  if(task.thread_id&&active.some(t=>t.thread_id===task.thread_id))return false;
  const locks=new Set(active.flatMap(t=>JSON.parse(t.resources) as string[]));
  return !(JSON.parse(task.resources) as string[]).some(r=>locks.has(r));
}
export class Scheduler {
  private timer?:NodeJS.Timeout; private queue=new PQueue({concurrency:config.MAX_CONCURRENT_TASKS});
  private stopped=false;
  constructor(private service:TaskService){}
  start(){this.timer=setInterval(()=>this.tick(),500);this.tick();}
  tick(){
    if(this.stopped||!this.service.desktop.ready)return;
    const blocked=new Set<string>();
    for(const task of this.service.store.list('QUEUED')){
      const user=`${task.tenant_key}:${task.user_id}`;
      if(blocked.has(user))continue;
      if(!canSchedule(task,this.service.store.active(),config.MAX_CONCURRENT_TASKS,config.MAX_TASKS_PER_USER)){blocked.add(user);continue;}
      this.service.store.transition(task.id,'STARTING',{started_at:Date.now()});
      void this.queue.add(()=>this.service.start(this.service.store.get(task.id)!)).catch(e=>log.error({taskId:task.id,error:safeError(e)},'调度异常'));
    }
    for(const task of this.service.store.active()){
      const expired=config.TASK_TIMEOUT_MS>0&&task.started_at&&Date.now()-task.started_at>config.TASK_TIMEOUT_MS;
      const requestExpired=this.service.store.db.prepare("SELECT 1 FROM pending_requests WHERE task_id=? AND status='PENDING' AND expires_at<? LIMIT 1").get(task.id,Date.now());
      if((expired||requestExpired)&&!task.cancel_requested){this.service.store.update(task.id,{cancel_requested:1});if(task.thread_id&&task.turn_id)void this.service.interrupt(task).catch(()=>{});}
    }
  }
  async stop(){this.stopped=true;clearInterval(this.timer);await this.queue.onIdle();}
}
