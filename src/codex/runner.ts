import {EventEmitter} from 'node:events';
import {createRequire} from 'node:module';
import {execFile,spawn} from 'node:child_process';
import {promisify} from 'node:util';
import {Codex,type CodexOptions,type ModelReasoningEffort,type ThreadEvent,type ThreadOptions,type UserInput} from '@openai/codex-sdk';
import {config} from '../config.js';

const run=promisify(execFile);
// The launcher script of @openai/codex, the package the SDK itself drives: it finds the native binary of this platform.
const launcher=()=>createRequire(import.meta.url).resolve('@openai/codex/bin/codex.js');
// Stored as a file under <project>/.codex, never in the OS keychain, so nothing about the login lives outside the project.
const LOGIN_STORE='cli_auth_credentials_store="file"';
// Whatever must not reach Codex, and the home it must use whatever the environment says.
const WITHHELD=new Set(['CODEX_HOME','CODEX_API_KEY','FEISHU_APP_SECRET']);

/** The environment of every Codex process: ours, minus the service's secrets, with Codex's home inside the project. */
export function codexEnv():Record<string,string>{
  const env:Record<string,string>={};
  for(const [key,value] of Object.entries(process.env))if(value!==undefined&&!WITHHELD.has(key.toUpperCase()))env[key]=value;
  return {...env,CODEX_HOME:config.codexHome};
}
/**
 * What Codex is configured with, passed with every run instead of written to a config file, so the project holds the only copy.
 * The features switched off would put one account, browser or machine in front of every Feishu user: connectors of the logged-in ChatGPT
 * account, Codex's own browser and computer control. Nothing else is added: no instructions, no tools.
 */
export function codexConfig():NonNullable<CodexOptions['config']>{
  return {cli_auth_credentials_store:'file',features:{apps:false,plugins:false,browser_use:false,in_app_browser:false,computer_use:false,memories:false}};
}
/** No limits: Codex may do anything on this machine and nobody is asked, so what fails is only what cannot be done. */
export function threadOptions(cwd:string):ThreadOptions{
  return {workingDirectory:cwd,skipGitRepoCheck:true,sandboxMode:'danger-full-access',approvalPolicy:'never',
    ...(config.CODEX_MODEL?{model:config.CODEX_MODEL}:{}),
    ...(config.CODEX_REASONING_EFFORT?{modelReasoningEffort:config.CODEX_REASONING_EFFORT as ModelReasoningEffort}:{})};
}
/** Runs the Codex CLI of this project with the project's own home, attached to the terminal (for `login` and the like). */
export function codexCli(args:string[]):Promise<number>{
  return new Promise((resolve,reject)=>{
    const child=spawn(process.execPath,[launcher(),'-c',LOGIN_STORE,...args],{env:codexEnv(),stdio:'inherit'});
    child.once('error',reject);child.once('close',code=>resolve(code??1));
  });
}

export interface TurnJob {threadId:string|null;cwd:string;input:UserInput[];signal:AbortSignal}
type CodexFactory=(options:CodexOptions)=>Pick<Codex,'startThread'|'resumeThread'>;
/** Runs the user's turns with the Codex SDK; one `codex exec` process per turn, all sharing the home in <project>/.codex. */
export class CodexRunner extends EventEmitter {
  ready=false; version=''; error='';
  constructor(private factory:CodexFactory=options=>new Codex(options)){super();}
  /** Whether Codex can take a task: its binary runs and the project has a login (or an API key in .env). */
  async probe(){
    const before=this.ready;
    try{
      const cli=(args:string[])=>run(process.execPath,[launcher(),...args],{env:codexEnv(),timeout:20000,windowsHide:true,encoding:'utf8'});
      // The version cannot change while the service runs, so it is asked for once.
      if(!this.version)this.version=(await cli(['--version'])).stdout.trim().replace(/^codex-cli\s+/,'');
      if(!config.CODEX_API_KEY)await cli(['-c',LOGIN_STORE,'login','status']).catch(()=>{throw Error('Codex 尚未登录：运行 start.cmd codex login，或在 .env 里设置 CODEX_API_KEY');});
      this.ready=true;this.error='';
    }catch(e){this.ready=false;this.error=(e as Error).message;}
    if(before!==this.ready)this.emit(this.ready?'online':'offline');
    return this.ready;
  }
  /** One turn of the user's conversation. `threadId` continues an existing chat; the id of a new one arrives in the thread.started event. */
  async *run(job:TurnJob):AsyncGenerator<ThreadEvent>{
    const codex=this.factory({env:codexEnv(),apiKey:config.CODEX_API_KEY||undefined,config:codexConfig()});
    const options=threadOptions(job.cwd);
    const thread=job.threadId?codex.resumeThread(job.threadId,options):codex.startThread(options);
    const {events}=await thread.runStreamed(job.input,{signal:job.signal});
    yield* events;
  }
  close(){this.removeAllListeners();}
}
