import {spawn,type ChildProcess} from 'node:child_process';
import fs from 'node:fs/promises';
import path from 'node:path';
import {config} from './config.js';
import {log,safeError} from './log.js';
import {codexEnv} from './codex/runner.js';

/**
 * Periodic jobs that plugins declare for work that is nobody's turn (audit-auto reads its review groups once a day, say). A plugin lists them
 * in lark-agent.json, next to its plugin.json:
 *
 *   {"jobs":[{"name":"rules-sync","everyMinutes":1440,"timeoutMinutes":30,"command":"node","args":["${PLUGIN_ROOT}/server/dist/audit.mjs","rules","sync"]}]}
 *
 * A job is a command run in the plugin's installed copy (${PLUGIN_ROOT}; `node` is the node that runs this service), the first time a little
 * after the service starts and then every `everyMinutes`; a run still going when the next is due is not started twice, and one that outlasts
 * `timeoutMinutes` is ended. Its environment is what Codex gets without a user (the service's own minus its secrets, Codex's home), plus the
 * bot's Feishu app and the folder plugins share (LARK_AGENT_SHARED_DIR): there is no user, so no user folder or Open ID. What it prints is
 * logged (the end of it); it is not sent to anybody.
 */
export interface Job {plugin:string;name:string;everyMinutes:number;timeoutMinutes:number;command:string;args:string[];cwd:string}
export interface JobState {plugin:string;name:string;everyMinutes:number;running:boolean;runs:number;lastStart?:number;lastEnd?:number;lastExit?:number|null;lastError?:string}
/** Where a plugin is: its name and its installed folder. */
export interface JobSource {name:string;path:string}

export const JOBS_FILE='lark-agent.json';
const NAME=/^[A-Za-z0-9][A-Za-z0-9_-]*$/;
const MAX_MINUTES=7*24*60;
const OUTPUT_KEPT=4000;

/** The jobs a plugin declares, and why the ones that cannot run are left out. A plugin without lark-agent.json has none. */
export async function declaredJobs(plugin:JobSource):Promise<{jobs:Job[];invalid:string[]}>{
  const jobs:Job[]=[],invalid:string[]=[];
  let declared:any;
  try{const text=await fs.readFile(path.join(plugin.path,JOBS_FILE),'utf8');declared=JSON.parse(text.charCodeAt(0)===0xFEFF?text.slice(1):text);}
  catch(e){if((e as NodeJS.ErrnoException).code==='ENOENT')return {jobs,invalid};return {jobs,invalid:[`${JOBS_FILE} 无法解析：${(e as Error).message}`]};}
  const expand=(value:string)=>value.includes('${PLUGIN_ROOT}')?path.normalize(value.replaceAll('${PLUGIN_ROOT}',plugin.path)):value;
  for(const [i,job] of (Array.isArray(declared?.jobs)?declared.jobs:[]).entries()){
    const name=typeof job?.name==='string'?job.name:'',every=job?.everyMinutes,timeout=job?.timeoutMinutes??30;
    const why=!NAME.test(name)?'name 缺失或含有不允许的字符':!Number.isInteger(every)||every<5||every>MAX_MINUTES?`everyMinutes 要是 5 到 ${MAX_MINUTES} 的整数`
      :!Number.isInteger(timeout)||timeout<1||timeout>MAX_MINUTES?`timeoutMinutes 要是 1 到 ${MAX_MINUTES} 的整数`
      :typeof job.command!=='string'||!job.command?'command 缺失':job.args!==undefined&&(!Array.isArray(job.args)||job.args.some((a:unknown)=>typeof a!=='string'))?'args 要是字符串数组'
      :jobs.some(j=>j.name===name)?'与前面的任务同名':'';
    if(why){invalid.push(`任务 ${name||`#${i+1}`}：${why}`);continue;}
    jobs.push({plugin:plugin.name,name,everyMinutes:every,timeoutMinutes:timeout,command:job.command==='node'?process.execPath:expand(job.command),
      args:(job.args??[]).map(expand),cwd:plugin.path});
  }
  return {jobs,invalid};
}

/** A job's environment (see above). What is not set is left out rather than passed empty. */
export function jobEnv():Record<string,string>{
  const given:Record<string,string>={FEISHU_APP_ID:config.FEISHU_APP_ID,FEISHU_APP_SECRET:config.FEISHU_APP_SECRET,LARK_AGENT_SHARED_DIR:config.shared};
  for(const name of Object.keys(given))if(!given[name])delete given[name];
  return {...codexEnv(),...given};
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
  constructor(private firstDelayMs=2*60000){}
  /** Starts the timers of these jobs (the service has installed the plugins by now). */
  start(jobs:Job[]){
    for(const job of jobs){
      const id=`${job.plugin}/${job.name}`;
      this.states.set(id,{plugin:job.plugin,name:job.name,everyMinutes:job.everyMinutes,running:false,runs:0});
      const first=setTimeout(()=>{
        void this.run(job);
        const every=setInterval(()=>{void this.run(job);},job.everyMinutes*60000);every.unref();this.timers.push(every);
      },this.firstDelayMs);
      first.unref();this.timers.push(first);
    }
    if(jobs.length)log.info({jobs:jobs.map(j=>`${j.plugin}/${j.name}（每 ${j.everyMinutes} 分钟）`)},'插件定时任务已就绪');
  }
  status():JobState[]{return [...this.states.values()].map(s=>({...s}));}
  /** One run of a job; a run of it still going means this one is skipped. Never rejects. */
  run(job:Job):Promise<void>{
    const id=`${job.plugin}/${job.name}`,state=this.states.get(id)??{plugin:job.plugin,name:job.name,everyMinutes:job.everyMinutes,running:false,runs:0};
    this.states.set(id,state);
    if(this.stopped)return Promise.resolve();
    const busy=this.running.get(id);
    if(busy){log.info({plugin:job.plugin,job:job.name},'插件定时任务上一次还没跑完，这次跳过');return busy;}
    const done=new Promise<void>(resolve=>{
      const started=Date.now();let output='',timedOut=false;
      Object.assign(state,{running:true,lastStart:started});
      const child=spawn(job.command,job.args,{cwd:job.cwd,env:jobEnv(),windowsHide:true,stdio:['ignore','pipe','pipe']});
      this.children.set(id,child);
      const keep=(chunk:Buffer)=>{output=(output+chunk.toString('utf8')).slice(-OUTPUT_KEPT);};
      child.stdout!.on('data',keep);child.stderr!.on('data',keep);
      const timer=setTimeout(()=>{timedOut=true;end(child);},job.timeoutMinutes*60000);timer.unref();
      const finish=(exit:number|null,error?:string)=>{
        clearTimeout(timer);this.children.delete(id);this.running.delete(id);
        const tail=safeError(new Error(output.trim().slice(-2000)));
        Object.assign(state,{running:false,lastEnd:Date.now(),lastExit:exit,runs:state.runs+1,lastError:error});
        if(error)log.warn({plugin:job.plugin,job:job.name,exit,ms:Date.now()-started,error,output:tail},'插件定时任务失败');
        else log.info({plugin:job.plugin,job:job.name,ms:Date.now()-started,output:tail.split('\n').at(-1)},'插件定时任务完成');
        resolve();
      };
      child.once('error',e=>finish(null,`启动失败：${safeError(e)}`));
      child.once('close',code=>{
        if(!this.children.has(id))return;
        finish(code,timedOut?`超过 ${job.timeoutMinutes} 分钟，已结束`:this.stopped?'服务停止，已结束':code===0?undefined:`退出码 ${code}`);
      });
    });
    this.running.set(id,done);
    return done;
  }
  /** No more runs; what is running is ended and waited for (a few seconds at most). */
  async stop(){
    this.stopped=true;for(const timer of this.timers)clearTimeout(timer);this.timers=[];
    for(const child of this.children.values())end(child);
    await Promise.race([Promise.all(this.running.values()),new Promise(resolve=>setTimeout(resolve,5000).unref())]);
  }
}
