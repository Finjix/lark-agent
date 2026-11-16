import {spawn,type ChildProcess} from 'node:child_process';
import fs from 'node:fs/promises';
import {config} from './config.js';
import {log,safeError} from './log.js';
import {skillEnv,venvPython} from './agent/environment.js';

/**
 * Commands run on a timer, for work that is nobody's turn (audit-auto reads its review groups once a day, say). They are listed in jobs.json
 * at the project root:
 *
 *   {"jobs":[{"name":"audit-rules-sync","everyMinutes":1440,"timeoutMinutes":30,"command":"python","args":["skills/audit-auto/scripts/audit.py","rules","sync"]}]}
 *
 * A job runs in the project folder (`python` is the project's Python, `node` the node that runs this service), the first time a little after
 * the service starts and then every `everyMinutes`; a run still going when the next is due is not started twice, and one that outlasts
 * `timeoutMinutes` is ended. Its environment is what the skills get without a user (see skillEnv): there is no user folder, Open ID or chat.
 * What it prints is logged (the end of it); it is not sent to anybody.
 */
export interface Job {name:string;everyMinutes:number;timeoutMinutes:number;command:string;args:string[]}
export interface JobState {name:string;everyMinutes:number;running:boolean;runs:number;lastStart?:number;lastEnd?:number;lastExit?:number|null;lastError?:string}

const NAME=/^[A-Za-z0-9][A-Za-z0-9_-]*$/;
const MAX_MINUTES=7*24*60;
const OUTPUT_KEPT=4000;
const COMMANDS:Record<string,()=>string>={python:venvPython,node:()=>process.execPath};

/** The jobs jobs.json lists, and why the ones that cannot run are left out. No file, no jobs. */
export async function declaredJobs(file=config.jobs):Promise<{jobs:Job[];invalid:string[]}>{
  const jobs:Job[]=[],invalid:string[]=[];
  let declared:any;
  try{const text=await fs.readFile(file,'utf8');declared=JSON.parse(text.charCodeAt(0)===0xFEFF?text.slice(1):text);}
  catch(e){if((e as NodeJS.ErrnoException).code==='ENOENT')return {jobs,invalid};return {jobs,invalid:[`jobs.json 无法解析：${(e as Error).message}`]};}
  for(const [i,job] of (Array.isArray(declared?.jobs)?declared.jobs:[]).entries()){
    const name=typeof job?.name==='string'?job.name:'',every=job?.everyMinutes,timeout=job?.timeoutMinutes??30;
    const why=!NAME.test(name)?'name 缺失或含有不允许的字符':!Number.isInteger(every)||every<5||every>MAX_MINUTES?`everyMinutes 要是 5 到 ${MAX_MINUTES} 的整数`
      :!Number.isInteger(timeout)||timeout<1||timeout>MAX_MINUTES?`timeoutMinutes 要是 1 到 ${MAX_MINUTES} 的整数`
      :typeof job.command!=='string'||!job.command?'command 缺失':job.args!==undefined&&(!Array.isArray(job.args)||job.args.some((a:unknown)=>typeof a!=='string'))?'args 要是字符串数组'
      :jobs.some(j=>j.name===name)?'与前面的任务同名':'';
    if(why){invalid.push(`任务 ${name||`#${i+1}`}：${why}`);continue;}
    jobs.push({name,everyMinutes:every,timeoutMinutes:timeout,command:COMMANDS[job.command]?.()??job.command,args:job.args??[]});
  }
  return {jobs,invalid};
}

/** Ends a job and whatever it started: on Windows a process's children outlive it unless the tree is ended. */
function end(child:ChildProcess){
  if(child.exitCode!==null||child.signalCode!==null||!child.pid)return;
  if(process.platform==='win32')spawn('taskkill',['/pid',String(child.pid),'/T','/F'],{windowsHide:true,stdio:'ignore'}).on('error',()=>child.kill());
  else child.kill('SIGKILL');
}

export class Jobs {
  private timers:NodeJS.Timeout[]=[];private states=new Map<string,JobState>();private children=new Map<string,ChildProcess>();
  private running=new Map<string,Promise<void>>();private stopped=false;
  constructor(private firstDelayMs=2*60000,private cwd=config.root){}
  start(jobs:Job[]){
    for(const job of jobs){
      this.states.set(job.name,{name:job.name,everyMinutes:job.everyMinutes,running:false,runs:0});
      const first=setTimeout(()=>{
        void this.run(job);
        const every=setInterval(()=>{void this.run(job);},job.everyMinutes*60000);every.unref();this.timers.push(every);
      },this.firstDelayMs);
      first.unref();this.timers.push(first);
    }
    if(jobs.length)log.info({jobs:jobs.map(j=>`${j.name}（每 ${j.everyMinutes} 分钟）`)},'定时任务已就绪');
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
      const child=spawn(job.command,job.args,{cwd:this.cwd,env:skillEnv(),windowsHide:true,stdio:['ignore','pipe','pipe']});
      this.children.set(job.name,child);
      const keep=(chunk:Buffer)=>{output=(output+chunk.toString('utf8')).slice(-OUTPUT_KEPT);};
      child.stdout!.on('data',keep);child.stderr!.on('data',keep);
      const timer=setTimeout(()=>{timedOut=true;end(child);},job.timeoutMinutes*60000);timer.unref();
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
    this.stopped=true;for(const timer of this.timers)clearTimeout(timer);this.timers=[];
    for(const child of this.children.values())end(child);
    await Promise.race([Promise.all(this.running.values()),new Promise(resolve=>setTimeout(resolve,5000).unref())]);
  }
}
