import fs from 'node:fs/promises';
import type {ThreadEvent,UserInput} from '@openai/codex-sdk';
import {Store} from '../storage/database.js';
import {sessionInputs} from '../feishu/inputs.js';
import {CodexRunner} from '../codex/runner.js';
import {Turn} from '../codex/turn.js';
import {codexInput} from '../codex/input.js';
import {generatedImages} from '../feishu/images.js';
import {userWorkspace,userDirectory} from '../layout.js';
import {config} from '../config.js';
import {terminal,waiting,BOT_DOWN,InputRejected,type Task,type IncomingMessage,type UserSession} from '../types.js';
import {safeError,log} from '../log.js';
import {prepareInputs,type ResourceDownloader} from '../feishu/inputs.js';
import {authorized} from '../access.js';
import {Steering} from './steering.js';

export const UNAVAILABLE=BOT_DOWN;
// What Codex says when the gateway key of the project is no longer accepted. That is the bot being unavailable, not something for the user to read.
const AUTH_FAILURE=/\b401\b|unauthorized|not logged in|log ?in again|sign ?in again|token.{0,20}(expired|revoked|invalid)/i;
// Clock skew allowance for the start check, and how late a message may arrive: live ones take a second or two, Feishu's redeliveries of what was missed while the service was down take much longer.
const STALE_GRACE_MS=1000,MAX_DELIVERY_LAG_MS=30000;
export class TaskService {
  private startedAt=Date.now();
  /** Feishu's clock minus this machine's (ms), kept current by the main loop; message times are Feishu's. */
  clockOffsetMs=0;
  /** One entry per task from the moment it starts until it is over: what stops its Codex process. */
  private runs=new Map<string,AbortController>();
  /** Tasks whose Codex turn is under way. */
  get running(){return this.runs.size;}
  /** Per unfinished task that messages sent meanwhile are handed to (see join): how they reach its turn. */
  private steering=new Map<string,Steering>();
  private steeringFor(taskId:string){let steering=this.steering.get(taskId);if(!steering)this.steering.set(taskId,steering=new Steering());return steering;}
  /** Users ("tenant:user") whose /new is under way, with how many. */
  private resetting=new Map<string,number>();
  private stopping=false;
  /** How long /new waits for a stopped Codex process to be gone before it ends the task regardless. */
  stopWaitMs=5000;
  /**
   * How long a Codex process may keep running after it reported the turn's result, or after the task was stopped. A command of the turn can
   * leave something running that holds the process open (a browser waiting for a QR scan, say): Codex then only exits once that ends, so after
   * this it is stopped and the task ends without it.
   */
  exitWaitMs=3000;
  constructor(public store:Store,public codex:CodexRunner,private downloader?:ResourceDownloader){}
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
    // The one thing the bot itself answers: Codex's own /new. Everything else, a slash included, is for Codex.
    if(message.text.trim()==='/new'&&!message.attachments?.length){await this.newConversation(message);return;}
    if(!message.text.trim()&&!message.attachments?.length)return;
    // Nothing is queued while Codex is down: the user is told at once and sends it again, instead of a backlog running later and answering many times.
    if(this.codex.ready===false&&!this.store.taskForMessage(message.tenant,message.message)){this.notice(message,UNAVAILABLE);return;}
    const resources=config.resources.filter(r=>r.regex.test(message.text)).map(r=>r.key);
    const result=this.store.create({tenant_key:message.tenant,user_id:message.user,chat_id:message.chat,source_message_id:message.message,
      instruction:message.text},resources,message.attachments);
    // A redelivered event of a message that is already a task (queued, running or handed over) changes nothing.
    if(!result.created)return;
    log.info({taskId:result.task.id},'任务已入队');
    // A message sent while the user's previous one is unfinished goes into that one's turn (Codex's steering), instead of waiting for a turn of its own.
    // Not into a task being stopped (/new, its time limit) or while /new is under way: it is then for what comes next. Nor when it needs a resource
    // lock that task does not hold: running on its own, it gets the lock from the scheduler.
    const previous=this.store.unfinished(message.tenant,message.user).filter(t=>t.id!==result.task.id&&t.status!=='STEERING').at(-1);
    const holds=(t:Task)=>{const held=JSON.parse(t.resources) as string[];return resources.every(r=>held.includes(r));};
    if(previous&&!previous.cancel_requested&&!this.paused(previous)&&holds(previous))
      void this.join(result.task,previous).catch(e=>log.warn({taskId:result.task.id,error:safeError(e)},'消息交给执行中的任务时出错'));
  }
  /** True while /new replaces the user's conversation: their messages wait, so that none starts in the conversation being left. */
  paused(task:Task){return this.resetting.has(`${task.tenant_key}:${task.user_id}`);}
  /**
   * Hands a message sent while the user's previous one is unfinished to that task's turn: Codex takes it in while it works (it reads it at its
   * next step, whatever it is doing) and the task answers both, on its card, where the message shows as a step. Until the turn has started the
   * message waits for it. One that Codex does not take (the turn ended first, or never started) goes back in the queue and runs on its own
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
    return codexInput(task.instruction,this.store.inputs(task.id));
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
  /** The user's workspace: Codex's working directory, where what it makes is delivered from, and where the attachments arrive. */
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
    if(this.runs.has(task.id))return;
    const abort=new AbortController();this.runs.set(task.id,abort);
    const steering=this.steeringFor(task.id);
    let turn:Turn|undefined;
    try{
      if(this.store.get(task.id)?.cancel_requested){this.store.transition(task.id,'CANCELLED');return;}
      const {session,folder}=await this.prepare(task);
      const current=this.store.get(task.id)!;
      if(current.cancel_requested||abort.signal.aborted||terminal(current.status)){if(!terminal(current.status))this.store.transition(task.id,this.stopping?'FAILED':'CANCELLED',this.stopping?{error:BOT_DOWN}:{});return;}
      const input=codexInput(current.instruction,this.store.inputs(task.id));
      // From here on the input has been handed over, and the task is never started a second time.
      this.store.event(task.id,'CODEX_SUBMITTED',{});
      turn=new Turn();steering.turn=turn;
      await this.execute(this.store.get(task.id)!,session,folder,input,turn,abort.signal,steering);
    }catch(e){
      const current=this.store.get(task.id);
      if(!current||terminal(current.status))return;
      if(abort.signal.aborted&&!this.stopping){
        // The user stopped it: what Codex had said by then stays on the card.
        await this.finish(current,turn,this.store.submitted(task.id)?'INTERRUPTED':'CANCELLED');
      }else{
        log.warn({taskId:task.id,error:safeError(e)},this.stopping?'服务停止，任务中断':'Codex 执行失败');
        await this.finish(current,turn,'FAILED',e instanceof InputRejected&&!this.stopping?e.message:BOT_DOWN);
      }
    }
    // Messages still waiting to be handed over now run on their own, after this task.
    finally{steering.close();this.steering.delete(task.id);this.runs.delete(task.id);}
  }
  /** Runs one turn and ends the task by its outcome. */
  private async execute(task:Task,session:UserSession,folder:string,input:UserInput[],turn:Turn,signal:AbortSignal,steering:Steering){
    let threadId=session.thread_id,received=false;
    for(let renewed=false;;){
      try{
        // The user's own folder (not the workspace in it) and Open ID are what plugins serve this turn with.
        const user={dir:userDirectory(this.store,session),openId:session.user_id};
        const done=new AbortController();let timer:NodeJS.Timeout|undefined,giveUp=()=>{};
        const gaveUp=new Promise<undefined>(resolve=>{giveUp=()=>resolve(undefined);});
        // Once the turn has reported its end, or the task was stopped (/new, its time limit, the service stopping), the events are waited for
        // only a little longer: the stream ends when Codex's output pipe closes, and on Windows what a command left running (a browser waiting
        // for a QR scan, say) holds that pipe too, killed process or not. What is still on its way then is dropped.
        const stop=()=>{timer??=setTimeout(()=>{done.abort();giveUp();},this.exitWaitMs);};
        signal.addEventListener('abort',stop,{once:true});if(signal.aborted)stop();
        const events=this.codex.run({threadId,cwd:folder,input,signal:AbortSignal.any([signal,done.signal]),user,steerable:steer=>steering.offer(steer)})[Symbol.asyncIterator]();
        try{
          for(;;){
            const next=events.next();
            const event=await Promise.race([next,gaveUp]);
            if(!event){next.catch(()=>{});break;}
            if(event.done)break;
            received=true;this.onEvent(task,session,turn,event.value);
            if(turn.state!=='running')stop();
          }
        }finally{
          clearTimeout(timer);signal.removeEventListener('abort',stop);
          // Left early (an event that could not be recorded, say), the turn still ends: returning the iterator runs the runner's cleanup, which
          // closes Codex. Otherwise its process would go on with the turn, and live on, after the task has failed.
          void events.return?.(undefined)?.catch(()=>{});
        }
        break;
      }catch(e){
        // After a failed turn the process exits with an error code, which the SDK throws; the turn's own report is what counts.
        if(turn.state!=='running'){log.debug({taskId:task.id,error:safeError(e)},'Codex 进程在本轮结束后以错误退出');break;}
        // Codex no longer has the chat this conversation continues (its home was emptied, say). Resuming it fails before anything is sent, so the task goes on in a new chat.
        if(!threadId||renewed||received||!/no rollout found/i.test(safeError(e)))throw e;
        log.warn({taskId:task.id,threadId},'Codex 里已没有原对话，改用新对话');
        session=this.store.renewSession(this.store.get(task.id)!,'missing');this.store.setSessionWorkspace(session.id,folder);
        threadId=null;renewed=true;
      }
    }
    if(turn.state==='running'){
      // Killed on request, the process may end without an error of its own.
      signal.throwIfAborted();
      log.warn({taskId:task.id},'Codex 没有给出本轮的结果就结束了');await this.finish(this.store.get(task.id)!,turn,'FAILED',BOT_DOWN);return;
    }
    if(turn.state==='failed')log.warn({taskId:task.id,error:turn.error},'Codex 本轮失败');
    await this.finish(this.store.get(task.id)!,turn,turn.state==='completed'?'COMPLETED':'FAILED',AUTH_FAILURE.test(turn.error)?BOT_DOWN:turn.error);
  }
  private onEvent(task:Task,session:UserSession,turn:Turn,event:ThreadEvent){
    if(event.type==='thread.started'){
      if(!session.thread_id)this.store.bindSession(session.id,event.thread_id);
      this.store.update(task.id,{thread_id:event.thread_id});
    }
    const changed=turn.apply(event);
    if(this.store.get(task.id)?.status==='STARTING')this.store.transition(task.id,'RUNNING');
    // The end of the turn is handled by finish, together with the images it made.
    const steps=JSON.stringify(turn.steps);
    if(changed&&turn.state==='running'&&this.store.get(task.id)!.steps!==steps){
      this.store.update(task.id,{progress:turn.progress,steps});this.store.enqueue(`view:${task.id}`,task.id,'VIEW',{});
    }
  }
  /** Ends the task. The images Codex generated during it are recorded first: the final transition is what delivers them. */
  private async finish(task:Task,turn:Turn|undefined,status:'COMPLETED'|'FAILED'|'INTERRUPTED'|'CANCELLED',error=''){
    const threadId=turn?.threadId||this.store.get(task.id)?.thread_id;
    if(threadId&&status!=='CANCELLED'){
      try{this.store.recordImages(task.id,await generatedImages(threadId,task.started_at??task.created_at));}
      catch(e){log.warn({taskId:task.id,error:safeError(e)},'读取生成的图片失败');}
    }
    this.store.transition(task.id,status,{result:turn?.answer??'',progress:turn?.progress??'',...(turn?{steps:JSON.stringify(turn.steps)}:{}),error});
    log.info({taskId:task.id,status},'任务结束');
  }
  /** Stops the task's Codex process; false when nothing is running it. */
  interrupt(task:Task){const run=this.runs.get(task.id);run?.abort();return !!run;}
  /** Service shutdown: every Codex process ends, and its task is finished as failed, so no card is left saying "running". */
  stop(){this.stopping=true;for(const run of this.runs.values())run.abort();}
}
