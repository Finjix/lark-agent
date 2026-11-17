import {spawn,type ChildProcess} from 'node:child_process';
import fs from 'node:fs/promises';
import path from 'node:path';
import {config} from '../config.js';
import {log,safeError} from '../log.js';
import {skillEnv,systemPython} from '../agent/environment.js';

/**
 * Commands run on a timer, for work that is nobody's turn (audit-auto reads its review groups once a day, say). A skill that needs one writes
 * it while it is used, into jobs.json of its folder in data/shared (audit-auto: data/shared/audit-auto/jobs.json, once a group is listened to):
 *
 *   {"jobs":[{"name":"audit-rules-sync","everyMinutes":1440,"timeoutMinutes":30,"command":"python","args":["scripts/audit.py","rules","sync"]}]}
 *
 * The files are read when the service starts. A job runs in the skill's folder, skills/<the same name> (`python` is the machine's Python,
 * `node` the node that runs this service), the first time a little after the service starts and then every `everyMinutes`; a run still going
 * when the next is due is not started twice, and one that outlasts `timeoutMinutes` is ended. Its environment is what the skills get without a
 * user (see skillEnv): there is no user folder, Open ID or chat. What it prints is logged (the end of it); it is not sent to anybody.
 */
export interface Job {name:string;everyMinutes:number;timeoutMinutes:number;command:string;args:string[];cwd:string}
export interface JobState {name:string;everyMinutes:number;running:boolean;runs:number;lastStart?:number;lastEnd?:number;lastExit?:number|null;lastError?:string}

const NAME=/^[A-Za-z0-9][A-Za-z0-9_-]*$/;
const MAX_MINUTES=7*24*60;
const OUTPUT_KEPT=4000;
const COMMANDS:Record<string,()=>string>={python:systemPython,node:()=>process.execPath};

/**
 * The jobs the skills wrote into data/shared/<skill>/jobs.json, and why the ones that cannot run are left out. No file, no jobs; a skill
 * folder without jobs.json has none.
 */
export async function declaredJobs(shared=config.shared,skills=config.skills):Promise<{jobs:Job[];invalid:string[]}>{
  const jobs:Job[]=[],invalid:string[]=[];
  const folders=(await fs.readdir(shared,{withFileTypes:true}).catch(()=>[])).filter(e=>e.isDirectory()).map(e=>e.name).sort();
  for(const skill of folders){
    const file=path.join(shared,skill,'jobs.json'),where=`${skill}/jobs.json`;
    let declared:any;
    try{const text=await fs.readFile(file,'utf8');declared=JSON.parse(text.charCodeAt(0)===0xFEFF?text.slice(1):text);}
    catch(e){if((e as NodeJS.ErrnoException).code!=='ENOENT')invalid.push(`${where} 无法解析：${(e as Error).message}`);continue;}
    const cwd=path.join(skills,skill);
    if(!await fs.stat(cwd).then(s=>s.isDirectory(),()=>false)){invalid.push(`${where}：skills/ 里没有技能 ${skill}，它的定时任务不运行`);continue;}
    for(const [i,job] of (Array.isArray(declared?.jobs)?declared.jobs:[]).entries()){
      const name=typeof job?.name==='string'?job.name:'',every=job?.everyMinutes,timeout=job?.timeoutMinutes??30;
      const why=!NAME.test(name)?'name 缺失或含有不允许的字符':!Number.isInteger(every)||every<5||every>MAX_MINUTES?`everyMinutes 要是 5 到 ${MAX_MINUTES} 的整数`
        :!Number.isInteger(timeout)||timeout<1||timeout>MAX_MINUTES?`timeoutMinutes 要是 1 到 ${MAX_MINUTES} 的整数`
        :typeof job.command!=='string'||!job.command?'command 缺失':job.args!==undefined&&(!Array.isArray(job.args)||job.args.some((a:unknown)=>typeof a!=='string'))?'args 要是字符串数组'
        :jobs.some(j=>j.name===name)?'与前面的任务同名':'';
      if(why){invalid.push(`${where} 任务 ${name||`#${i+1}`}：${why}`);continue;}
      jobs.push({name,everyMinutes:every,timeoutMinutes:timeout,command:COMMANDS[job.command]?.()??job.command,args:job.args??[],cwd});
    }
  }
  return {jobs,invalid};
}

/** Ends a job and whatever it started: on Windows a process's children outlive it unless the tree is ended. */
export function endTree(child:ChildProcess){
  if(child.exitCode!==null||child.signalCode!==null||!child.pid)return;
  if(process.platform==='win32')spawn('taskkill',['/pid',String(child.pid),'/T','/F'],{windowsHide:true,stdio:'ignore'}).on('error',()=>child.kill());
  else child.kill('SIGKILL');
}

export class Jobs {
  /** The jobs scheduled now, each with its definition (to tell a changed one) and its timer. */
  private scheduled=new Map<string,{definition:string;timer:NodeJS.Timeout}>();
  private states=new Map<string,JobState>();private children=new Map<string,ChildProcess>();
  private running=new Map<string,Promise<void>>();private stopped=false;
  constructor(private firstDelayMs=2*60000){}
  start(jobs:Job[]){this.update(jobs);}
  /**
   * Schedules what is declared now: a new job (a skill wrote it while the service runs) first runs a little later, then on its interval; a
   * job no longer declared, or declared differently, is no longer scheduled (a run of it still going finishes) and a changed one starts over.
   */
  update(jobs:Job[]){
    if(this.stopped)return;
    const wanted=new Map(jobs.map(job=>[job.name,job]));
    for(const [name,{definition,timer}] of this.scheduled){
      if(JSON.stringify(wanted.get(name))===definition)continue;
      clearTimeout(timer);clearInterval(timer);this.scheduled.delete(name);
      if(!wanted.has(name)){this.states.delete(name);log.info({job:name},'定时任务已取消');}
    }
    const added:Job[]=[];
    for(const job of jobs){
      if(this.scheduled.has(job.name))continue;
      this.states.set(job.name,{...this.states.get(job.name),name:job.name,everyMinutes:job.everyMinutes,running:this.running.has(job.name),runs:this.states.get(job.name)?.runs??0});
      const entry={definition:JSON.stringify(job),timer:setTimeout(()=>{
        void this.run(job);
        entry.timer=setInterval(()=>{void this.run(job);},job.everyMinutes*60000);entry.timer.unref();
      },this.firstDelayMs)};
      entry.timer.unref();this.scheduled.set(job.name,entry);added.push(job);
    }
    if(added.length)log.info({jobs:added.map(j=>`${j.name}（每 ${j.everyMinutes} 分钟）`)},'定时任务已就绪');
  }
  status():JobState[]{return [...this.states.values()].map(s=>({...s}));}
  /** One run of a job; a run of it still going means this one is skipped. Never rejects. */
  run(job:Job):Promise<void>{
    const state=this.states.get(job.name)??{name:job.name,everyMinutes:job.everyMinutes,running:false,runs:0};
    this.states.set(job.name,state);
    if(this.stopped)return Promise.resolve();
    const busy=this.running.get(job.name);
    if(busy){log.info({job:job.name},'定时任务上一次还没跑完，这次跳过');return busy;}
    const done=new Promise<void>(resolve=>{
      const started=Date.now();let output='',timedOut=false;
      Object.assign(state,{running:true,lastStart:started});
      const child=spawn(job.command,job.args,{cwd:job.cwd,env:skillEnv(),windowsHide:true,stdio:['ignore','pipe','pipe']});
      this.children.set(job.name,child);
      const keep=(chunk:Buffer)=>{output=(output+chunk.toString('utf8')).slice(-OUTPUT_KEPT);};
      child.stdout!.on('data',keep);child.stderr!.on('data',keep);
      const timer=setTimeout(()=>{timedOut=true;endTree(child);},job.timeoutMinutes*60000);timer.unref();
      const finish=(exit:number|null,error?:string)=>{
        clearTimeout(timer);this.children.delete(job.name);this.running.delete(job.name);
        const tail=safeError(new Error(output.trim().slice(-2000)));
        Object.assign(state,{running:false,lastEnd:Date.now(),lastExit:exit,runs:state.runs+1,lastError:error});
        if(error)log.warn({job:job.name,exit,ms:Date.now()-started,error,output:tail},'定时任务失败');
        else log.info({job:job.name,ms:Date.now()-started,output:tail.split('\n').at(-1)},'定时任务完成');
        resolve();
      };
      child.once('error',e=>finish(null,`启动失败：${safeError(e)}`));
      child.once('close',code=>{
        if(!this.children.has(job.name))return;
        finish(code,timedOut?`超过 ${job.timeoutMinutes} 分钟，已结束`:this.stopped?'服务停止，已结束':code===0?undefined:`退出码 ${code}`);
      });
    });
    this.running.set(job.name,done);
    return done;
  }
  /** No more runs; what is running is ended and waited for (a few seconds at most). */
  async stop(){
    this.stopped=true;for(const {timer} of this.scheduled.values())clearTimeout(timer);this.scheduled.clear();
    for(const child of this.children.values())endTree(child);
    await Promise.race([Promise.all(this.running.values()),new Promise(resolve=>setTimeout(resolve,5000).unref())]);
  }
}
