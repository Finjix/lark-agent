import fs from 'node:fs/promises';
import {Store} from '../storage/database.js';
import {AgentRunner,SessionMissing} from '../agent/runner.js';
import {Turn,type TurnEvent} from '../agent/turn.js';
import {agentInput,type AgentInput} from '../agent/input.js';
import {userWorkspace,userDirectory,sessionInputs} from '../users/layout.js';
import {config} from '../config.js';
import {terminal,waiting,BOT_DOWN,InputRejected,type Task,type IncomingMessage,type UserSession} from '../types.js';
import {safeError,log} from '../log.js';
import {prepareInputs,type ResourceDownloader} from '../feishu/inputs.js';
import {authorized} from '../users/access.js';
import {Steering} from './steering.js';

// What the model's provider says when the ChatGPT login is no longer accepted. That is the bot being unavailable, not something for the user to read.
const AUTH_FAILURE=/\b401\b|unauthorized|not logged in|no api key|log ?in again|sign ?in again|token.{0,20}(expired|revoked|invalid)/i;
// Clock skew allowance for the start check, and how late a message may arrive: live ones take a second or two, Feishu's redeliveries of what was missed while the service was down take much longer.
const STALE_GRACE_MS=1000,MAX_DELIVERY_LAG_MS=30000;
export class TaskService {
  private startedAt=Date.now();
  /** Feishu's clock minus this machine's (ms), kept current by the main loop; message times are Feishu's. */
  clockOffsetMs=0;
  /** One entry per task from the moment it starts until it is over: what stops its turn. */
  private runs=new Map<string,AbortController>();
  /** Tasks whose turn is under way. */
  get running(){return this.runs.size;}
  /** Per unfinished task that messages sent meanwhile are handed to (see join): how they reach its turn. */
  private steering=new Map<string,Steering>();
  private steeringFor(taskId:string){let steering=this.steering.get(taskId);if(!steering)this.steering.set(taskId,steering=new Steering());return steering;}
  /** Users ("tenant:user") whose /new is under way, with how many. */
  private resetting=new Map<string,number>();
  /** Whether each chat is private or a group, from its messages (the task does not keep it): what skills are told with the chat. */
  private chatTypes=new Map<string,string>();
  private stopping=false;
  /** How long /new waits for a stopped turn to be over before it ends the task regardless. */
  stopWaitMs=5000;
  /**
   * How long a turn may take to wind down after it reported its result, or after the task was stopped: a command of the turn can leave
   * something running (a browser waiting for a QR scan, say). After this the task ends without waiting for it.
   */
  exitWaitMs=3000;
  /** How long a turn given up on may still take to end (its session let go of) before it no longer holds its user and resources. */
  lingerMaxMs=60000;
  /**
   * Tasks that are over while their turn is not yet: given up on (see exitWaitMs), the pi session may still be ending. Until it has, the
   * scheduler counts them as running, so the user's next task (same workspace, same chat file) and one wanting their resources wait.
   */
  private lingering=new Map<string,Task>();
  held():Task[]{return [...this.lingering.values()];}
  private hold(task:Task,ended:Promise<unknown>){
    let over=false;
    this.lingering.set(task.id,task);
    const limit=new Promise(resolve=>setTimeout(resolve,this.lingerMaxMs).unref());
    void ended.then(()=>{over=true;},()=>{over=true;});
    void Promise.race([ended.catch(()=>{}),limit]).then(()=>{
      if(!over)log.error({taskId:task.id},'放弃的一轮迟迟没有结束，不再为它占着用户和资源');
      this.lingering.delete(task.id);
    });
  }
  constructor(public store:Store,public agent:AgentRunner,private downloader?:ResourceDownloader){}
  notice(message:IncomingMessage,text:string){this.store.enqueue(`notice:${message.tenant}:${message.message}`,null,'NOTICE',{chat:message.chat,message:message.message,text});}
  authorized(message:IncomingMessage){return authorized(message);}
  private async refreshSessionName(session:UserSession){
    if(!this.downloader?.getUserName)return;
    try{const name=await this.downloader.getUserName(session.user_id);if(name)this.store.nameSession(session.id,name);}
    catch(e){log.warn({error:safeError(e)},'用户名称查询失败，会话和任务继续使用现有名称');}
  }
  async receive(message:IncomingMessage){
    // Feishu may redeliver events that arrived while this service was not running; those are stale and must not run now.
    const lagMs=Date.now()+this.clockOffsetMs-(message.createdAt??0);
    if(message.createdAt&&(message.createdAt<this.startedAt+this.clockOffsetMs-STALE_GRACE_MS||lagMs>MAX_DELIVERY_LAG_MS)){log.info({message:message.message,lagMs},'忽略服务不可用期间发送、事后才送达的消息');return;}
    // Without this there is no way to learn what to put in the allowlist, so the refusal says who the user is.
    if(!this.authorized(message)){this.notice(message,`尚未获授权，请联系管理员开通。\n你的 Open ID：${message.user}\n租户：${message.tenant}`);return;}
    if(message.error){this.notice(message,message.error);return;}
    // Queued now, it would be dropped at the next start without a word: the user is told instead.
    if(this.stopping){this.notice(message,BOT_DOWN);return;}
    // The one thing the bot itself answers: /new. Everything else, a slash included, is for the agent.
    if(message.text.trim()==='/new'&&!message.attachments?.length){await this.newConversation(message);return;}
    if(!message.text.trim()&&!message.attachments?.length)return;
    // Nothing is queued while the agent is down: the user is told at once and sends it again, instead of a backlog running later and answering many times.
    if(this.agent.ready===false&&!this.store.taskForMessage(message.tenant,message.message)){this.notice(message,BOT_DOWN);return;}
    if(message.chatType)this.chatTypes.set(message.chat,message.chatType);
    const resources=config.resources.filter(r=>r.regex.test(message.text)).map(r=>r.key);
    const result=this.store.create({tenant_key:message.tenant,user_id:message.user,chat_id:message.chat,source_message_id:message.message,
      instruction:message.text},resources,message.attachments);
    // A redelivered event of a message that is already a task (queued, running or handed over) changes nothing.
    if(!result.created)return;
    log.info({taskId:result.task.id},'任务已入队');
    void this.announce(message).catch(()=>{});
    // A message sent while the user's previous one is unfinished goes into that one's turn (steering), instead of waiting for a turn of its own.
    // Not into a task being stopped (/new, its time limit) or while /new is under way: it is then for what comes next. Nor when it needs a resource
    // lock that task does not hold: running on its own, it gets the lock from the scheduler. Nor into a task of another chat (the user writes in
    // a group while their private chat's task runs): its answer would go to that chat, and the turn's skills would take it for that chat's.
    const previous=this.store.unfinished(message.tenant,message.user).filter(t=>t.id!==result.task.id&&t.status!=='STEERING').at(-1);
    const holds=(t:Task)=>{const held=JSON.parse(t.resources) as string[];return resources.every(r=>held.includes(r));};
    if(previous&&previous.chat_id===message.chat&&!previous.cancel_requested&&!this.paused(previous)&&holds(previous))
      void this.join(result.task,previous).catch(e=>log.warn({taskId:result.task.id,error:safeError(e)},'消息交给执行中的任务时出错'));
  }
  /**
   * Shows a submitted task on the console: who (their Feishu name, cached by the client, else the one their chat has, else the Open ID),
   * where, and the start of what they wrote. It never holds up the task.
   */
  private async announce(message:IncomingMessage){
    const looked=await this.downloader?.getUserName?.(message.user).catch(()=>undefined);
    const who=looked||this.store.knownName(message.tenant,message.user)||message.user;
    const first=message.text.trim().split(/\r?\n/)[0]??'',text=first.length>60?first.slice(0,60)+'…':first;
    const files=message.attachments?.length?`（附件 ${message.attachments.length} 个）`:'';
    log.console(`${who}${message.chatType==='group'?'（群聊）':''} 提交任务：${text||'（无文字）'}${files}`);
  }
  /** True while /new replaces the user's conversation: their messages wait, so that none starts in the conversation being left. */
  paused(task:Task){return this.resetting.has(`${task.tenant_key}:${task.user_id}`);}
  /**
   * Hands a message sent while the user's previous one is unfinished to that task's turn: the agent takes it in while it works (it reads it
   * after its current step) and the task answers both, on its card, where the message shows as a step. Until the turn has started the
   * message waits for it. One the agent does not take (the turn ended first, or never started) goes back in the queue and runs on its own
   * once the task is over, so nothing a user sends is lost. Messages are handed over in the order they came.
   */
  private async join(task:Task,target:Task){
    // Claimed first: the scheduler leaves a message alone while it is being handed over.
    if(!this.store.claim(task.id,'QUEUED','STEERING'))return;
    const steering=this.steeringFor(target.id),over=()=>terminal(this.store.get(target.id)?.status??'COMPLETED');
    try{
      await steering.serial(async()=>{
        if(!await steering.ready(over))throw Error('前一条消息的任务已经结束');
        const input=await this.joiningInput(this.store.get(task.id)!);
        // Receiving the attachments can outlast the turn: the way in is asked for again once they are here.
        const steer=await steering.ready(over);
        if(!steer)throw Error('前一条消息的任务已经结束');
        await steer(input);
      });
    }catch(e){
      // Attachments it cannot take (too large, say) are refused at once, as for a message of its own, not after the running task.
      if(e instanceof InputRejected){log.info({taskId:task.id,error:e.message},'补充的消息的附件被拒收');this.store.transition(task.id,'FAILED',{error:e.message});return;}
      log.info({taskId:task.id,into:target.id,error:safeError(e)},'消息没能交给执行中的任务，改为单独执行');
      // Back in the queue, unless it ended meanwhile (/new cancels it).
      this.store.claim(task.id,'STEERING','QUEUED');return;
    }finally{
      // A task that ended without ever running leaves no turn to close its entry.
      if(!this.runs.has(target.id)&&over())this.steering.delete(target.id);
    }
    this.store.transition(task.id,'STEERED');this.store.event(target.id,'STEERED',{taskId:task.id});
    log.info({taskId:task.id,into:target.id},'消息已交给执行中的任务');
    const turn=steering.turn,current=this.store.get(target.id);
    if(turn&&current&&!terminal(current.status)){
      turn.steered(`steer:${task.id}`,task.instruction,this.store.inputs(task.id).map(i=>i.name));
      this.store.update(target.id,{steps:JSON.stringify(turn.steps)});this.store.enqueue(`view:${target.id}`,target.id,'VIEW',{});
    }
  }
  /** What a joining message gives the turn: its text and attachments as for a message of its own, received into the workspace the turn works in. */
  private async joiningInput(task:Task){
    if(this.store.inputs(task.id).length){
      if(!this.downloader)throw Error('附件下载服务不可用');
      const session=this.store.sessionFor(task),folder=userWorkspace(this.store,session);
      this.store.update(task.id,{workspace_path:folder,thread_id:session.thread_id});
      await prepareInputs(this.store,this.store.get(task.id)!,this.downloader,sessionInputs(folder));
    }
    return agentInput(task.instruction,this.store.inputs(task.id));
  }
  /** /new: forget the conversation. A task still running is stopped first, so this is also how a user gets going again when one hangs. */
  private async newConversation(message:IncomingMessage){
    const {tenant,user}=message,key=`${tenant}:${user}`,unfinished=this.store.unfinished(tenant,user);
    // Until the conversation is replaced nothing of this user starts (see paused): what they send meanwhile is for the new one.
    this.resetting.set(key,(this.resetting.get(key)??0)+1);
    try{
      // Waiting messages go first, so that none of them starts in the old conversation while the running task is being stopped.
      for(const task of unfinished)if(waiting(task.status))this.store.transition(task.id,'CANCELLED',{error:''});
      for(const task of unfinished){
        if(waiting(task.status))continue;
        this.store.update(task.id,{cancel_requested:1});
        if(this.interrupt(task))for(const until=Date.now()+this.stopWaitMs;this.runs.has(task.id)&&Date.now()<until;)await new Promise(r=>setTimeout(r,50));
        // Ends it here if the process did not (a no-op for a task that has ended): the user's queue must not stay blocked.
        this.store.transition(task.id,'INTERRUPTED',{error:''});
      }
      this.notice(message,this.store.resetSession(tenant,user)||unfinished.length?'已开始新的对话，之前的内容不再带入。':'还没有对话，直接发送任务即可。');
    }finally{const left=this.resetting.get(key)!-1;if(left)this.resetting.set(key,left);else this.resetting.delete(key);}
  }
  /** The user's workspace: the agent's working directory, where what it makes is delivered from, and where the attachments arrive. */
  private async prepare(task:Task){
    const named=this.store.sessionFor(task);await this.refreshSessionName(named);
    const session=this.store.session(named.id)!,folder=userWorkspace(this.store,session);
    await fs.mkdir(folder,{recursive:true});
    if(session.workspace_path!==folder)this.store.setSessionWorkspace(session.id,folder);
    this.store.update(task.id,{workspace_path:folder,thread_id:session.thread_id});
    if(this.store.inputs(task.id).length){
      if(!this.downloader)throw Error('附件下载服务不可用');
      this.store.update(task.id,{progress:'正在接收附件'});this.store.enqueue(`view:${task.id}`,task.id,'VIEW',{});
      await prepareInputs(this.store,task,this.downloader,sessionInputs(folder));
      this.store.update(task.id,{progress:''});
    }
    return {session:this.store.session(session.id)!,folder};
  }
  async start(task:Task){
    if(this.stopping||this.runs.has(task.id))return;
    const abort=new AbortController();this.runs.set(task.id,abort);
    const steering=this.steeringFor(task.id);
    let turn:Turn|undefined;
    try{
      if(this.store.get(task.id)?.cancel_requested){this.store.transition(task.id,'CANCELLED');return;}
      const {session,folder}=await this.prepare(task);
      const current=this.store.get(task.id)!;
      if(current.cancel_requested||abort.signal.aborted||terminal(current.status)){if(!terminal(current.status))this.store.transition(task.id,this.stopping?'FAILED':'CANCELLED',this.stopping?{error:BOT_DOWN}:{});return;}
      const input=await agentInput(current.instruction,this.store.inputs(task.id));
      // From here on the input has been handed over, and the task is never started a second time.
      this.store.event(task.id,'SUBMITTED',{});
      turn=new Turn();steering.turn=turn;
      await this.execute(this.store.get(task.id)!,session,folder,input,turn,abort.signal,steering);
    }catch(e){
      const current=this.store.get(task.id);
      if(!current||terminal(current.status))return;
      if(abort.signal.aborted&&!this.stopping){
        // The user stopped it: what the agent had said by then stays on the card.
        this.finish(current,turn,this.store.submitted(task.id)?'INTERRUPTED':'CANCELLED');
      }else{
        log.warn({taskId:task.id,error:safeError(e)},this.stopping?'服务停止，任务中断':'智能体执行失败');
        this.finish(current,turn,'FAILED',e instanceof InputRejected&&!this.stopping?e.message:BOT_DOWN);
      }
    }
    // Messages still waiting to be handed over now run on their own, after this task.
    finally{steering.close();this.steering.delete(task.id);this.runs.delete(task.id);}
  }
  /** Runs one turn and ends the task by its outcome. */
  private async execute(task:Task,session:UserSession,folder:string,input:AgentInput,turn:Turn,signal:AbortSignal,steering:Steering){
    let threadId=session.thread_id,received=false;
    for(let renewed=false;;){
      try{
        // The user's own folder (not the workspace in it), Open ID and the chat the message came from are what skills serve this turn with.
        const user={dir:userDirectory(this.store,session),openId:session.user_id,chat:task.chat_id,chatType:this.chatTypes.get(task.chat_id)};
        const done=new AbortController();let timer:NodeJS.Timeout|undefined,giveUp=()=>{};
        const gaveUp=new Promise<undefined>(resolve=>{giveUp=()=>resolve(undefined);});
        // Once the turn has reported its end, or the task was stopped (/new, its time limit, the service stopping), the events are waited for
        // only a little longer: winding down can hang on what a command left running (a browser waiting for a QR scan, say). What is still on
        // its way then is dropped.
        const stop=()=>{timer??=setTimeout(()=>{done.abort();giveUp();},this.exitWaitMs);};
        signal.addEventListener('abort',stop,{once:true});if(signal.aborted)stop();
        const events=this.agent.run({sessionId:threadId,cwd:folder,input,signal:AbortSignal.any([signal,done.signal]),abandon:done.signal,user,steerable:steer=>steering.offer(steer)})[Symbol.asyncIterator]();
        let over=false;
        try{
          for(;;){
            const next=events.next();
            const event=await Promise.race([next,gaveUp]);
            if(!event){next.catch(()=>{});break;}
            if(event.done){over=true;break;}
            received=true;this.onEvent(task,session,turn,event.value);
            if(turn.state!=='running')stop();
          }
        }finally{
          clearTimeout(timer);signal.removeEventListener('abort',stop);
          // Left early (an event that could not be recorded, say), the turn still ends: returning the iterator runs the runner's cleanup, which
          // stops the turn. Otherwise the agent would go on with it after the task has failed. Until that cleanup is done, the task still holds
          // its user and resources (see lingering).
          const ending=Promise.resolve(events.return?.(undefined));
          if(over)void ending.catch(()=>{});else this.hold(task,ending);
        }
        break;
      }catch(e){
        // Something failing as the turn winds down after it reported its end: the turn's own report is what counts.
        if(turn.state!=='running'){log.debug({taskId:task.id,error:safeError(e)},'本轮结束后收尾出错');break;}
        // The chat this conversation continues is gone (pi's home was emptied, or it is a chat of the Codex used before). That is found before
        // anything is sent, so the task goes on in a new chat.
        if(!threadId||renewed||received||!(e instanceof SessionMissing))throw e;
        log.warn({taskId:task.id,threadId},'原对话已不存在，改用新对话');
        session=this.store.renewSession(this.store.get(task.id)!,'missing');this.store.setSessionWorkspace(session.id,folder);
        threadId=null;renewed=true;
      }
    }
    if(turn.state==='running'){
      // Stopped on request, the turn may end without an error of its own.
      signal.throwIfAborted();
      log.warn({taskId:task.id},'智能体没有给出本轮的结果就结束了');this.finish(this.store.get(task.id)!,turn,'FAILED',BOT_DOWN);return;
    }
    if(turn.state==='failed')log.warn({taskId:task.id,error:turn.error},'智能体本轮失败');
    this.finish(this.store.get(task.id)!,turn,turn.state==='completed'?'COMPLETED':'FAILED',AUTH_FAILURE.test(turn.error)?BOT_DOWN:turn.error);
  }
  private onEvent(task:Task,session:UserSession,turn:Turn,event:TurnEvent){
    if(event.type==='session'){
      if(!session.thread_id)this.store.bindSession(session.id,event.id);
      this.store.update(task.id,{thread_id:event.id});
    }
    const changed=turn.apply(event);
    if(this.store.get(task.id)?.status==='STARTING')this.store.transition(task.id,'RUNNING');
    // The end of the turn is handled by finish.
    const steps=JSON.stringify(turn.steps);
    if(changed&&turn.state==='running'&&this.store.get(task.id)!.steps!==steps){
      this.store.update(task.id,{progress:turn.progress,steps});this.store.enqueue(`view:${task.id}`,task.id,'VIEW',{});
    }
  }
  /** Ends the task; the final transition is what delivers the reply and the files. */
  private finish(task:Task,turn:Turn|undefined,status:'COMPLETED'|'FAILED'|'INTERRUPTED'|'CANCELLED',error=''){
    this.store.transition(task.id,status,{result:turn?.answer??'',progress:turn?.progress??'',...(turn?{steps:JSON.stringify(turn.steps)}:{}),error});
    log.info({taskId:task.id,status},'任务结束');
  }
  /** Stops the task's turn; false when nothing is running it. */
  interrupt(task:Task){const run=this.runs.get(task.id);run?.abort();return !!run;}
  /** Service shutdown: every turn ends, and its task is finished as failed, so no card is left saying "running". */
  stop(){this.stopping=true;for(const run of this.runs.values())run.abort();}
}
