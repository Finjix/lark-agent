import {EventEmitter} from 'node:events';
import {createRequire} from 'node:module';
import {execFile,spawn} from 'node:child_process';
import {promisify} from 'node:util';
import type {CodexOptions,ModelReasoningEffort,ThreadEvent,ThreadItem,ThreadOptions,UserInput} from '@openai/codex-sdk';
import {config} from '../config.js';
import {pluginConfig,PLUGIN_ENV,type Plugin,type PluginUser} from '../plugins.js';
import {spawnAppServer,type AppServer,type ServerOptions} from './appserver.js';

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

/** The config overrides as command-line flags, flattened the way the Codex SDK passes them to `codex exec` (a dotted key per value, TOML values). */
export function configArgs(value:NonNullable<CodexOptions['config']>):string[]{
  const toml=(v:unknown):string=>typeof v==='string'?JSON.stringify(v):typeof v==='number'||typeof v==='boolean'?String(v)
    :Array.isArray(v)?`[${v.map(toml).join(', ')}]`:`{${Object.entries(v as object).filter(([,x])=>x!==undefined).map(([k,x])=>`${/^[A-Za-z0-9_-]+$/.test(k)?k:JSON.stringify(k)} = ${toml(x)}`).join(', ')}}`;
  const args:string[]=[];
  const walk=(node:Record<string,unknown>,prefix:string)=>{
    if(prefix&&!Object.keys(node).length){args.push('--config',`${prefix}={}`);return;}
    for(const [key,child] of Object.entries(node)){
      if(child===undefined)continue;const at=prefix?`${prefix}.${key}`:key;
      if(child&&typeof child==='object'&&!Array.isArray(child))walk(child as Record<string,unknown>,at);else args.push('--config',`${at}=${toml(child)}`);
    }
  };
  walk(value as Record<string,unknown>,'');return args;
}

/** What one turn of the app-server says, in the SDK's terms (what `codex exec --experimental-json` printed, and what Turn folds). */
export function threadItem(item:any):ThreadItem|undefined{
  const status=(s:unknown)=>s==='inProgress'?'in_progress' as const:s==='completed'?'completed' as const:'failed' as const;
  switch(item?.type){
    case 'agentMessage':return {id:item.id,type:'agent_message',text:String(item.text??'')};
    case 'commandExecution':return {id:item.id,type:'command_execution',command:String(item.command??''),aggregated_output:String(item.aggregatedOutput??''),
      ...(typeof item.exitCode==='number'?{exit_code:item.exitCode}:{}),status:status(item.status)};
    case 'fileChange':return {id:item.id,type:'file_change',changes:(item.changes??[]).map((c:any)=>({path:String(c.path),kind:c.kind?.type??c.kind})),
      status:item.status==='failed'||item.status==='declined'?'failed':'completed'};
    case 'mcpToolCall':return {id:item.id,type:'mcp_tool_call',server:String(item.server),tool:String(item.tool),arguments:item.arguments,
      ...(item.result?{result:{content:item.result.content??[],structured_content:item.result.structuredContent}}:{}),
      ...(item.error?{error:{message:String(item.error.message??'')}}:{}),status:status(item.status)};
    case 'webSearch':return {id:item.id,type:'web_search',query:String(item.query??'')};
    // The user's own messages (the first and the steered ones), reasoning, images Codex looked at and the like are no steps of a card.
    default:return undefined;
  }
}
export function turnEvents(turnId:string){
  let planShown=false;
  return function*(method:string,params:any):Generator<ThreadEvent>{
    switch(method){
      case 'item/started':case 'item/completed':{const item=threadItem(params?.item);if(item)yield {type:method==='item/started'?'item.started':'item.completed',item};return;}
      // The to-do list Codex keeps: one item, updated in place.
      case 'turn/plan/updated':{
        const item:ThreadItem={id:`plan-${turnId}`,type:'todo_list',items:(params?.plan??[]).map((step:any)=>({text:String(step?.step??''),completed:step?.status==='completed'}))};
        yield {type:planShown?'item.updated':'item.started',item};planShown=true;return;
      }
      // Codex's own notices (a reconnect, say); the turn still ends with its own result.
      case 'error':yield {type:'error',message:String(params?.error?.message??'')};return;
      case 'turn/completed':{
        const turn=params?.turn;
        // Usage is kept nowhere here.
        if(turn?.status==='completed')yield {type:'turn.completed',usage:{input_tokens:0,cached_input_tokens:0,cache_write_input_tokens:0,output_tokens:0,reasoning_output_tokens:0}};
        else if(turn?.status==='failed')yield {type:'turn.failed',error:{message:String(turn.error?.message||'Codex 本轮失败')}};
        // Interrupted: only ever because the task was stopped, which its abort signal says.
        return;
      }
    }
  };
}
/** The user's input in the app-server's terms. */
const serverInput=(input:UserInput[])=>input.map(part=>part.type==='text'?{type:'text',text:part.text}:{type:'localImage',path:part.path});

/** `user` is whom the turn is for; plugins need it (the turn's environment carries their folder and Open ID). */
export interface TurnJob {
  threadId:string|null;cwd:string;input:UserInput[];signal:AbortSignal;user?:PluginUser;
  /**
   * Told once the turn has started, with what hands more of the user's input to it (Codex's steering: the model reads it at its next step,
   * whatever it is doing), and told again with nothing once the turn can no longer take any. A hand-over Codex refuses (the turn has just
   * ended) rejects.
   */
  steerable?:(steer:((input:UserInput[])=>Promise<void>)|undefined)=>void;
}
type Connect=(options:ServerOptions)=>AppServer;
/**
 * Runs the user's turns through `codex app-server`: one process per turn, with that user's environment, all sharing the home in
 * <project>/.codex. Its chats are the ones `codex exec` kept there before. A turn of the app-server can take more input while it runs, which is
 * how a user's message sent meanwhile reaches it.
 */
export class CodexRunner extends EventEmitter {
  ready=false; version=''; error='';
  /** The plugins installed from plugins/ (set at start); every turn of a user gets them. */
  plugins:Plugin[]=[];
  constructor(private connect:Connect=spawnAppServer){super();}
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
    const options=threadOptions(job.cwd);
    const args=[...configArgs(codexConfig(this.plugins)),...(options.modelReasoningEffort?['--config',`model_reasoning_effort="${options.modelReasoningEffort}"`]:[])];
    const env={...codexEnv(this.plugins.length?job.user:undefined),...(config.CODEX_API_KEY?{CODEX_API_KEY:config.CODEX_API_KEY}:{})};
    const server=this.connect({args,env,signal:job.signal});
    try{
      await server.request('initialize',{clientInfo:{name:'lark-agent',version:this.version||'0'}});
      server.notify('initialized');
      const settings={cwd:options.workingDirectory,sandbox:options.sandboxMode,approvalPolicy:options.approvalPolicy,...(options.model?{model:options.model}:{})};
      // A resumed chat is not sent back whole: only its id is needed.
      const {thread}=job.threadId?await server.request('thread/resume',{threadId:job.threadId,...settings,excludeTurns:true}):await server.request('thread/start',settings);
      yield {type:'thread.started',thread_id:thread.id};
      const {turn}=await server.request('turn/start',{threadId:thread.id,input:serverInput(job.input)});
      yield {type:'turn.started'};
      job.steerable?.(async input=>{await server.request('turn/steer',{threadId:thread.id,expectedTurnId:turn.id,input:serverInput(input)});});
      const events=turnEvents(turn.id);
      for(;;){
        const next=await server.notifications.next();
        if(next.done){job.signal.throwIfAborted();throw Error(server.exitReason??'Codex 没有给出本轮的结果就结束了');}
        const {method,params}=next.value,of=params?.turnId??params?.turn?.id;
        if(of!==undefined&&of!==turn.id)continue;
        yield* events(method,params);
        if(method==='turn/completed')return;
      }
    }finally{job.steerable?.(undefined);server.close();}
  }
  close(){this.removeAllListeners();}
}
