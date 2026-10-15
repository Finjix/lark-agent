import {config} from '../config.js';
import {TaskService} from './service.js';
import type {Task} from '../types.js';
import {log,safeError} from '../log.js';
export function canSchedule(task:Task,active:Task[]){
  // Each user's messages share one Desktop conversation and must retain their order.
  if(active.some(t=>t.tenant_key===task.tenant_key&&t.user_id===task.user_id))return false;
  if(task.thread_id&&active.some(t=>t.thread_id===task.thread_id))return false;
  const locks=new Set(active.flatMap(t=>JSON.parse(t.resources) as string[]));
  return !(JSON.parse(task.resources) as string[]).some(r=>locks.has(r));
}
export class Scheduler {
  private timer?:NodeJS.Timeout; private starts=new Set<Promise<void>>();
  private stopped=false;
  constructor(private service:TaskService){}
  start(){this.timer=setInterval(()=>this.tick(),500);this.tick();}
  tick(){
    if(this.stopped||!this.service.desktop.ready)return;
    const blocked=new Set<string>();
    const active=this.service.store.active();
    for(const task of this.service.store.list('QUEUED')){
      const user=`${task.tenant_key}:${task.user_id}`;
      if(blocked.has(user))continue;
      if(!canSchedule(task,active)){blocked.add(user);continue;}
      this.service.store.transition(task.id,'STARTING',{started_at:Date.now()});
      active.push(this.service.store.get(task.id)!);
      const start=Promise.resolve().then(()=>this.service.start(this.service.store.get(task.id)!)).catch(e=>log.error({taskId:task.id,error:safeError(e)},'调度异常'));
      this.starts.add(start);void start.finally(()=>this.starts.delete(start));
    }
    for(const task of this.service.store.active()){
      const expired=config.TASK_TIMEOUT_MS>0&&task.started_at&&Date.now()-task.started_at>config.TASK_TIMEOUT_MS;
      const requestExpired=this.service.store.db.prepare("SELECT 1 FROM pending_requests WHERE task_id=? AND status='PENDING' AND expires_at<? LIMIT 1").get(task.id,Date.now());
      if((expired||requestExpired)&&!task.cancel_requested){this.service.store.update(task.id,{cancel_requested:1});if(task.thread_id&&task.turn_id)void this.service.interrupt(task).catch(()=>{});}
    }
  }
  async stop(){this.stopped=true;clearInterval(this.timer);await Promise.all(this.starts);}
}
