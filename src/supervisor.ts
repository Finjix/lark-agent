import {spawn,execFile} from 'node:child_process';
import {readFile} from 'node:fs/promises';
import path from 'node:path';
import {config} from './config.js';
import {log,safeError} from './log.js';

/** What the supervisor needs of the service process. */
export interface Child {
  pid?:number; exitCode:number|null; signalCode:string|null;
  once(event:'exit',listener:(code:number|null,signal:string|null)=>void):unknown;
}
export interface SupervisorOptions {
  /** Starts the service (`main.js serve`). */
  start:()=>Child;
  /** The service's /health answer, or undefined when nothing answers in time. */
  health:()=>Promise<any>;
  /** Ends the process and everything it started (Codex runs as its children). */
  kill:(child:Child)=>void;
  /** Asks the service to stop safely, as `start.cmd stop` does. */
  shutdown:()=>Promise<void>;
  checkMs?:number; graceMs?:number; strikes?:number; staleMs?:number; stopWaitMs?:number;
  restartMs?:number; maxRestartMs?:number; steadyMs?:number;
}

/**
 * Keeps the service running: it is started as a child process and asked for /health every `checkMs`. A service that stops answering
 * (`strikes` checks in a row: its event loop is frozen or it hangs) or whose own round of checks has not finished for `staleMs` is killed
 * and started again; one that exits on its own is started again too, after a pause that grows while it keeps failing soon after starting.
 * A service that ends with code 0 was stopped on purpose (`start.cmd stop`, Ctrl+C), and the supervisor ends with it.
 * What a killed service left unfinished is settled by the next one at its start, as after any unclean exit.
 */
export class Supervisor {
  private stopping=false;private child?:Child;private wake?:()=>void;
  private checkMs:number;private graceMs:number;private strikes:number;private staleMs:number;private stopWaitMs:number;
  private restartMs:number;private maxRestartMs:number;private steadyMs:number;
  constructor(private options:SupervisorOptions){
    this.checkMs=options.checkMs??20000;this.graceMs=options.graceMs??120000;this.strikes=options.strikes??4;
    this.staleMs=options.staleMs??5*60000;this.stopWaitMs=options.stopWaitMs??30000;
    this.restartMs=options.restartMs??2000;this.maxRestartMs=options.maxRestartMs??60000;this.steadyMs=options.steadyMs??5*60000;
  }
  async run(){
    // A second copy would only fail to take the port and be restarted over and over.
    if(await this.options.health()){log.error({port:config.HEALTH_PORT},'已有服务在运行（健康检查端口有应答），本次不再启动');return 1;}
    log.info({port:config.HEALTH_PORT},'守护进程已启动');
    let quick=0;
    while(!this.stopping){
      const startedAt=Date.now(),child=this.child=this.options.start();
      log.info({servicePid:child.pid},'服务进程已启动');
      const {code,signal,killed}=await this.watch(child);
      this.child=undefined;
      if(this.stopping)break;
      if(!killed&&code===0){log.info({servicePid:child.pid},'服务已停止，守护进程随之退出');return 0;}
      quick=Date.now()-startedAt<this.steadyMs?quick+1:0;
      const delay=Math.min(this.maxRestartMs,this.restartMs*2**Math.max(0,quick-1));
      log.warn({servicePid:child.pid,code,signal,restartInMs:delay},killed?'已结束无响应的服务进程，稍后重启':'服务进程意外退出，稍后重启');
      await new Promise<void>(resolve=>{const timer=setTimeout(resolve,delay);this.wake=()=>{clearTimeout(timer);resolve();};});
      this.wake=undefined;
    }
    log.info('守护进程已停止');return 0;
  }
  /** Stops the service safely, or kills it after `stopWaitMs`, and ends the supervisor. */
  stop(){
    if(this.stopping)return;this.stopping=true;this.wake?.();
    const child=this.child;if(!child)return;
    log.info({servicePid:child.pid},'守护进程正在停止服务');
    void this.options.shutdown().catch(()=>{});
    const timer=setTimeout(()=>{if(alive(child)){log.warn({servicePid:child.pid},'服务未在时限内停止，强制结束');this.options.kill(child);}},this.stopWaitMs);
    timer.unref?.();
  }
  private watch(child:Child){
    return new Promise<{code:number|null;signal:string|null;killed:boolean}>(resolve=>{
      const startedAt=Date.now();let killed=false,answered=false,strikes=0,busy=false;
      const end=(fields:object,reason:string)=>{killed=true;log.error({servicePid:child.pid,...fields},reason);this.options.kill(child);};
      const check=async()=>{
        // Once killed, the kill is repeated until the process is gone.
        if(killed){this.options.kill(child);return;}
        if(busy||this.stopping)return;busy=true;
        try{
          const health=await this.options.health();
          if(!alive(child)||killed||this.stopping)return;
          if(!health){
            // It may still be starting (installing plugins comes before the first round of checks, not before /health).
            if(!answered&&Date.now()-startedAt<this.graceMs)return;
            if(++strikes<this.strikes){log.warn({servicePid:child.pid,strikes},'服务健康检查无应答');return;}
            end({strikes},'服务进程无响应，强制结束');return;
          }
          answered=true;strikes=0;
          if(typeof health.pollAgeMs==='number'&&health.pollAgeMs>this.staleMs)end({pollAgeMs:health.pollAgeMs},'服务内部检查已停滞，强制结束');
        }catch(e){log.warn({error:safeError(e)},'健康检查出错');}
        finally{busy=false;}
      };
      const timer=setInterval(()=>{void check();},this.checkMs);
      child.once('exit',(code,signal)=>{clearInterval(timer);resolve({code,signal,killed});});
    });
  }
}
const alive=(child:Child)=>child.exitCode===null&&child.signalCode===null;

/** `start`: runs `main.js serve` under a {@link Supervisor}. */
export async function supervise(){
  const base=`http://127.0.0.1:${config.HEALTH_PORT}`;
  const supervisor=new Supervisor({
    // The service shares this console; its console output goes through a worker thread (log.ts), so a stalled console cannot freeze it.
    start:()=>spawn(process.execPath,[...process.execArgv,process.argv[1]!,'serve'],{stdio:['ignore','inherit','inherit']}),
    health:async()=>{
      try{const response=await fetch(`${base}/health`,{signal:AbortSignal.timeout(10000)});return response.ok?await response.json():undefined;}
      catch{return undefined;}
    },
    kill:child=>{
      if(!child.pid)return;
      if(process.platform==='win32')execFile('taskkill.exe',['/pid',String(child.pid),'/T','/F'],{windowsHide:true},()=>{});
      else try{process.kill(child.pid,'SIGKILL');}catch{}
    },
    shutdown:async()=>{
      const token=await readFile(path.join(config.data,'control.token'),'utf8');
      await fetch(`${base}/shutdown`,{method:'POST',headers:{authorization:`Bearer ${token}`},signal:AbortSignal.timeout(5000)});
    },
  });
  // Ctrl+C reaches the service as well (same console); either way it stops safely and the supervisor does not restart it.
  for(const signal of ['SIGINT','SIGTERM','SIGHUP'] as const)process.once(signal,()=>supervisor.stop());
  return supervisor.run();
}
