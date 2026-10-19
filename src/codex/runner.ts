import {EventEmitter} from 'node:events';
import {createRequire} from 'node:module';
import {execFile,spawn} from 'node:child_process';
import {promisify} from 'node:util';
import {Codex,type CodexOptions,type ModelReasoningEffort,type ThreadEvent,type ThreadOptions,type UserInput} from '@openai/codex-sdk';
import {config} from '../config.js';
import {pluginConfig,PLUGIN_ENV,type Plugin,type PluginUser} from '../plugins.js';

const run=promisify(execFile);
// The launcher script of @openai/codex, the package the SDK itself drives: it finds the native binary of this platform.
const launcher=()=>createRequire(import.meta.url).resolve('@openai/codex/bin/codex.js');
// Stored as a file under <project>/.codex, never in the OS keychain, so nothing about the login lives outside the project.
const LOGIN_STORE='cli_auth_credentials_store="file"';
// Whatever must not reach Codex, and the home it must use whatever the environment says. (What plugins are given for a user's turn is set per turn, never inherited from the service.)
const WITHHELD=new Set(['CODEX_HOME','CODEX_API_KEY','FEISHU_APP_SECRET','LARK_AGENT_USER_DIR','LARK_AGENT_USER_OPEN_ID']);

/**
 * The environment of every Codex process: ours, minus the service's secrets, with Codex's home inside the project. For a turn of a user while
 * plugins are installed (`user`) it also carries what the plugins are given (PLUGIN_ENV): the user's folder and Open ID, and the bot's
 * Feishu app including its secret. That is the one case the secret reaches Codex; see README.
 */
export function codexEnv(user?:PluginUser):Record<string,string>{
  const env:Record<string,string>={};
  for(const [key,value] of Object.entries(process.env))if(value!==undefined&&!WITHHELD.has(key.toUpperCase()))env[key]=value;
  const given:Record<string,string>=user?{LARK_AGENT_USER_DIR:user.dir,LARK_AGENT_USER_OPEN_ID:user.openId,FEISHU_APP_ID:config.FEISHU_APP_ID,FEISHU_APP_SECRET:config.FEISHU_APP_SECRET}:{};
  // A value that is not set is left out rather than passed empty.
  for(const name of PLUGIN_ENV)if(!given[name])delete given[name];
  return {...env,...given,CODEX_HOME:config.codexHome};
}
/**
 * What Codex is configured with, passed with every run instead of written to a config file, so the project holds the only copy.
 * Two features are switched off because everything here is shared by all Feishu users: `apps` (the connectors of the logged-in ChatGPT account, on
 * by default) and `memories` (off by default, kept off so that what Codex remembers from one user's chats can never reach another's: the home is
 * shared). Nothing else is added: no instructions, no tools. Plugins are the exception, and only the ones dropped into plugins/ (see plugins.ts):
 * with any installed, Codex's plugin feature is on and their MCP servers are registered.
 */
export function codexConfig(plugins:Plugin[]=[]):NonNullable<CodexOptions['config']>{
  return {cli_auth_credentials_store:'file',features:{apps:false,plugins:plugins.length>0,memories:false},...pluginConfig(plugins)};
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

/** Runs the Codex CLI of this project to completion (for `plugin add` and the like) and returns what it printed. */
export async function codexOutput(args:string[],env:Record<string,string>=codexEnv()):Promise<string>{
  return (await run(process.execPath,[launcher(),...args],{env,timeout:300000,windowsHide:true,encoding:'utf8',maxBuffer:16*1024*1024})).stdout;
}

/** `user` is whom the turn is for; plugins need it (the turn's environment carries their folder and Open ID). */
export interface TurnJob {threadId:string|null;cwd:string;input:UserInput[];signal:AbortSignal;user?:PluginUser}
type CodexFactory=(options:CodexOptions)=>Pick<Codex,'startThread'|'resumeThread'>;
/** Runs the user's turns with the Codex SDK; one `codex exec` process per turn, all sharing the home in <project>/.codex. */
export class CodexRunner extends EventEmitter {
  ready=false; version=''; error='';
  /** The plugins installed from plugins/ (set at start); every turn of a user gets them. */
  plugins:Plugin[]=[];
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
    const codex=this.factory({env:codexEnv(this.plugins.length?job.user:undefined),apiKey:config.CODEX_API_KEY||undefined,config:codexConfig(this.plugins)});
    const options=threadOptions(job.cwd);
    const thread=job.threadId?codex.resumeThread(job.threadId,options):codex.startThread(options);
    const {events}=await thread.runStreamed(job.input,{signal:job.signal});
    yield* events;
  }
  close(){this.removeAllListeners();}
}
