import {readdirSync} from 'node:fs';
import path from 'node:path';
import {
  createAgentSession,createBashToolDefinition,createExtensionRuntime,loadSkillsFromDir,ModelRuntime,SessionManager,SettingsManager,VERSION,
  type AgentSession,type AgentSessionEvent,type ResourceLoader,type Skill,type ToolDefinition,
} from '@earendil-works/pi-coding-agent';
import type {Model} from '@earendil-works/pi-ai';
import {authFile,config} from '../config.js';
import {log} from '../log.js';
import {skillEnv,type SkillUser} from './environment.js';
import {FEISHU_TOOLS,feishuTools} from './feishu.js';
import {agentInstructions} from './instructions.js';
import type {AgentInput} from './input.js';
import type {TurnEvent} from './turn.js';

/** The chat a task was to continue is not among pi's sessions (its home was emptied, or it is a chat of the Codex this bot used before). */
export class SessionMissing extends Error {constructor(id:string){super(`找不到对话 ${id}`);this.name='SessionMissing';}}
/** A hand-over the turn did not take: it ended first. */
export class SteerRefused extends Error {constructor(){super('本轮已经结束，没有收下补充的消息');this.name='SteerRefused';}}

export type Steer=(input:AgentInput)=>Promise<void>;
/** `user` is whom the turn is for; the skills need it (their scripts are told the user's folder, Open ID and chat). */
export interface TurnJob {
  sessionId:string|null;cwd:string;input:AgentInput;signal:AbortSignal;user?:SkillUser;
  /**
   * Told once the turn has started, with what hands more of the user's input to it (pi's steering: the model reads it after its current step),
   * and told again with nothing once the turn can no longer take any. A hand-over resolves once the turn has taken the message, and rejects
   * with SteerRefused when the turn ends first.
   */
  steerable?:(steer:Steer|undefined)=>void;
  /**
   * Gives up on the turn: it ends at once (the session is let go of and disposed) without waiting for pi to settle, which a command that hangs
   * on what it started (a browser waiting for a scan, say) can keep from happening. The turn should have been stopped (`signal`) first.
   */
  abandon?:AbortSignal;
}
export interface RunnerOptions {
  /** Where the ChatGPT login lives; tests pass their own runtime. */
  runtime?:()=>Promise<ModelRuntime>;
  provider?:string;model?:string;thinking?:typeof config.AGENT_THINKING;
  skillsDir?:string;sessionsDir?:string;
  /** The Feishu module's feishu.py the Feishu tools run (tests pass a stand-in). */
  feishu?:string;
}

const textOf=(content:unknown)=>Array.isArray(content)?content.flatMap((c:any)=>c?.type==='text'&&typeof c.text==='string'?[c.text]:[]).join('\n'):typeof content==='string'?content:'';
/** A queue an event callback fills and an async iterator drains. */
class Channel<T> {
  private items:T[]=[];private waiting?:()=>void;private closed=false;
  push(item:T){this.items.push(item);this.waiting?.();}
  close(){this.closed=true;this.waiting?.();}
  async *drain():AsyncGenerator<T>{
    for(;;){
      while(this.items.length)yield this.items.shift()!;
      if(this.closed)return;
      await new Promise<void>(resolve=>{this.waiting=()=>{this.waiting=undefined;resolve();};});
    }
  }
}

/**
 * Runs the users' turns with the pi coding agent, in this process: one pi session per turn, made for that turn's workspace and user and
 * disposed of at its end, continuing the user's chat (a JSONL file in <project>/data/pi/sessions). Every turn gets the skills in <project>/skills,
 * pi's read, bash, edit and write tools (bash with the user's environment, see skillEnv), the agent's own Feishu tools (see feishu.ts) and the
 * instructions of agentInstructions; nothing of
 * the machine's own pi (~/.pi) is read. The model is the configured one, reached with the ChatGPT login of `start.cmd login`.
 */
export class AgentRunner {
  ready=false;error='';readonly version=VERSION;
  skills:Skill[]=[];
  private runtime?:Promise<ModelRuntime>;
  readonly provider:string;readonly modelId:string;readonly thinking:typeof config.AGENT_THINKING;
  private skillsDir:string;readonly sessionsDir:string;
  constructor(private options:RunnerOptions={}){
    this.provider=options.provider??config.AGENT_PROVIDER;this.modelId=options.model??config.AGENT_MODEL;this.thinking=options.thinking??config.AGENT_THINKING;
    this.skillsDir=options.skillsDir??config.skills;this.sessionsDir=options.sessionsDir??path.join(config.agentHome,'sessions');
  }
  /** pi's models and credentials, made once: the ChatGPT login is read from (and refreshed tokens are written to) <project>/data/pi/auth.json. */
  models():Promise<ModelRuntime>{
    return this.runtime??=(this.options.runtime??(()=>ModelRuntime.create({authPath:authFile(),modelsPath:null,refreshOnCreate:false})))()
      .catch(e=>{this.runtime=undefined;throw e;});
  }
  /** The skills in skills/, read again so a skill added or changed is used from the next turn on. Problems with one are logged, the rest load. */
  loadSkills(){
    const {skills,diagnostics}=loadSkillsFromDir({dir:this.skillsDir,source:'project'});
    for(const d of diagnostics)log.warn({diagnostic:d},'技能有问题');
    return this.skills=skills;
  }
  private async model():Promise<Model<any>>{
    const model=(await this.models()).getModel(this.provider,this.modelId);
    if(!model)throw Error(`找不到模型 ${this.provider}/${this.modelId}（.env 的 AGENT_PROVIDER / AGENT_MODEL）`);
    return model;
  }
  /** Whether a task can run: the model is known and its provider has a login. */
  async probe(){
    try{
      await this.model();
      const auth=await (await this.models()).checkAuth(this.provider);
      if(!auth)throw Error(`还没有登录 ChatGPT（${this.provider}），请运行 start.cmd login`);
      this.ready=true;this.error='';
    }catch(e){this.ready=false;this.error=(e as Error).message;}
    return this.ready;
  }
  private resources(skills:Skill[]):ResourceLoader{
    const prompt=agentInstructions(skills);
    return {
      getExtensions:()=>({extensions:[],errors:[],runtime:createExtensionRuntime()}),getSkills:()=>({skills,diagnostics:[]}),
      getPrompts:()=>({prompts:[],diagnostics:[]}),getThemes:()=>({themes:[],diagnostics:[]}),getAgentsFiles:()=>({agentsFiles:[]}),
      getSystemPrompt:()=>prompt,getSystemPromptSource:()=>undefined,getAppendSystemPrompt:()=>[],getAppendSystemPromptSources:()=>[],
      extendResources:()=>{},reload:async()=>{},
    };
  }
  private sessionManager(job:TurnJob){
    if(!job.sessionId)return SessionManager.create(job.cwd,this.sessionsDir);
    // Found by its id alone (pi names the file <time>_<id>.jsonl): pi's own lookup also wants the folder the chat was started in, so a moved
    // project would lose every chat. It goes on in the workspace of now.
    const id=job.sessionId,name=(()=>{try{return readdirSync(this.sessionsDir).find(f=>f.endsWith(`_${id}.jsonl`));}catch{return undefined;}})();
    if(!name)throw new SessionMissing(id);
    return SessionManager.open(path.join(this.sessionsDir,name),this.sessionsDir,job.cwd);
  }
  private async session(job:TurnJob):Promise<AgentSession>{
    const [model,modelRuntime]=await Promise.all([this.model(),this.models()]);
    const {session}=await createAgentSession({
      cwd:job.cwd,agentDir:config.agentHome,model,thinkingLevel:this.thinking,modelRuntime,resourceLoader:this.resources(this.loadSkills()),
      tools:['read','bash','edit','write',...FEISHU_TOOLS],
      // pi's own bash, in place of the default one, so that every command has the user's environment; and Feishu as this user.
      customTools:[createBashToolDefinition(job.cwd,{spawnHook:context=>({...context,env:skillEnv(job.user,context.env)})}) as unknown as ToolDefinition,
        ...feishuTools({cwd:job.cwd,user:job.user,entry:this.options.feishu})],
      sessionManager:this.sessionManager(job),
      // Steered messages go in together; nothing warms the cache in the background (every request counts against the subscription).
      settingsManager:SettingsManager.inMemory({compaction:{enabled:true},retry:{enabled:true,maxRetries:3},steeringMode:'all',cacheWarming:'off',enableSkillCommands:false}),
    });
    return session;
  }
  /** One turn of the user's conversation. `sessionId` continues an existing chat; the id of a new one is the first event. */
  async *run(job:TurnJob):AsyncGenerator<TurnEvent>{
    job.signal.throwIfAborted();
    const session=await this.session(job),events=new Channel<TurnEvent>();
    // `settled`: pi says the turn is over. `finished`: this run is, settled or not (it may have failed before it started).
    let settled=false,finished=false,prompted=false,messages=0,last:any;
    // A tool's end does not repeat its arguments.
    const args=new Map<string,Record<string,unknown>>();
    // Hand-overs waiting for the turn to take them, in the order they were queued. pi takes queued messages in that order, and each one it takes
    // starts as a user message after the turn's own prompt. Not matched by text: pi rewrites a message as it takes it (it notes how it resized
    // an image, it expands /skill:).
    const handed:{taken:()=>void;refused:(e:Error)=>void}[]=[];
    const unsubscribe=session.subscribe((event:AgentSessionEvent)=>{
      switch(event.type){
        // A stop that came while the turn was getting ready (pi ignores an abort before its run is under way).
        case 'agent_start':if(job.signal.aborted)void session.abort();return;
        case 'message_start':
          if(event.message.role!=='user')return;
          if(!prompted){prompted=true;return;}
          handed.shift()?.taken();
          return;
        case 'message_end':{
          if(event.message.role!=='assistant')return;
          last=event.message;const text=textOf(event.message.content).trim();
          if(text)events.push({type:'message',id:`m${++messages}`,text,commentary:event.message.content.some((c:any)=>c.type==='toolCall')});
          return;
        }
        case 'tool_execution_start':
          args.set(event.toolCallId,event.args??{});
          events.push({type:'tool',id:event.toolCallId,name:event.toolName,args:event.args??{},status:'running'});return;
        case 'tool_execution_end':{
          const exit=event.result?.structuredContent?.exit_code;
          events.push({type:'tool',id:event.toolCallId,name:event.toolName,args:args.get(event.toolCallId)??{},status:event.isError?'failed':'done',
            output:textOf(event.result?.content),...(typeof exit==='number'?{exit}:{})});
          return;
        }
        case 'agent_settled':settled=true;for(const h of handed.splice(0))h.refused(new SteerRefused());return;
      }
    });
    const abort=()=>{void session.abort();};
    job.signal.addEventListener('abort',abort,{once:true});
    // Given up on: the events end now, and so does the wait for the run below.
    let abandoned=()=>{};const given=new Promise<void>(resolve=>{abandoned=resolve;});
    const abandon=()=>{events.close();abandoned();};
    job.abandon?.addEventListener('abort',abandon,{once:true});if(job.abandon?.aborted)abandon();
    try{
      job.signal.throwIfAborted();
      yield {type:'session',id:session.sessionId};
      const run=session.prompt(job.input.text,{images:job.input.images,expandPromptTemplates:false}).then(()=>{
        job.signal.throwIfAborted();
        if(last?.stopReason==='error')events.push({type:'failed',error:String(last.errorMessage||'模型本轮失败')});
        else if(last?.stopReason==='aborted')throw Error(String(last.errorMessage||'本轮被中断'));
        else events.push({type:'completed'});
      }).finally(()=>events.close());
      run.catch(()=>{});
      job.steerable?.(async input=>{
        // Queued before the run has finished, pi delivers it (it runs on while messages are queued); queued after, it is refused at settling.
        if(settled||finished)throw new SteerRefused();
        let hand!:typeof handed[number];
        const taken=new Promise<void>((resolve,reject)=>{hand={taken:resolve,refused:reject};});
        handed.push(hand);
        try{await session.steer(input.text,input.images.length?input.images:undefined);}
        catch(e){handed.splice(handed.indexOf(hand),1);throw e;}
        await taken;
      });
      yield* events.drain();
      if(job.abandon?.aborted)throw Error('本轮已放弃，未等智能体结束');
      await Promise.race([run,given]);
      if(job.abandon?.aborted)throw Error('本轮已放弃，未等智能体结束');
    }finally{
      finished=true;job.steerable?.(undefined);job.signal.removeEventListener('abort',abort);job.abandon?.removeEventListener('abort',abandon);unsubscribe();
      for(const h of handed.splice(0))h.refused(new SteerRefused());
      // An abort that hangs as the run does is not waited for once the turn is given up on.
      if(!settled)await Promise.race([session.abort().catch(()=>{}),given]);
      session.dispose();
    }
  }
  close(){void this.runtime?.then(r=>r.settleCredentialWrites()).catch(()=>{});}
}
