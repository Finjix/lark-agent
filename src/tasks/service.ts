import fs from 'node:fs/promises';
import type {ThreadEvent,UserInput} from '@openai/codex-sdk';
import {Store} from '../storage/database.js';
import {sessionInputs} from '../feishu/inputs.js';
import {CodexRunner} from '../codex/runner.js';
import {Turn} from '../codex/turn.js';
import {codexInput} from '../codex/input.js';
import {generatedImages} from '../feishu/images.js';
import {userWorkspace} from '../layout.js';
import {config} from '../config.js';
import {terminal,BOT_DOWN,type Task,type IncomingMessage,type UserSession} from '../types.js';
import {safeError,log} from '../log.js';
import {prepareInputs,type ResourceDownloader} from '../feishu/inputs.js';
import {authorized} from '../access.js';

export const UNAVAILABLE=BOT_DOWN;
// What Codex says when the login of the project is no longer accepted. That is the bot being unavailable, not something for the user to read.
const AUTH_FAILURE=/\b401\b|unauthorized|not logged in|log ?in again|sign ?in again|token.{0,20}(expired|revoked|invalid)/i;
// Clock skew allowance for the start check, and how late a message may arrive: live ones take a second or two, Feishu's redeliveries of what was missed while the service was down take much longer.
const STALE_GRACE_MS=1000,MAX_DELIVERY_LAG_MS=30000;
export class TaskService {
  private startedAt=Date.now();
  /** Feishu's clock minus this machine's (ms), kept current by the main loop; message times are Feishu's. */
  clockOffsetMs=0;
  /** One entry per task from the moment it starts until it is over: what stops its Codex process. */
  private runs=new Map<string,AbortController>();
  private stopping=false;
  /** How long /new waits for a stopped Codex process to be gone before it ends the task regardless. */
  stopWaitMs=5000;
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
    // One input runs at a time. Anything sent while the user's previous input is still unfinished gets no result back, so it is not an input at all:
    // it is dropped without a reply and never queued. (A redelivered event of the message already running lands here too and is equally harmless.)
    if(this.store.busy(message.tenant,message.user)){log.info({message:message.message,user:message.user},'忽略执行期间收到的输入');return;}
    // Nothing is queued while Codex is down: the user is told at once and sends it again, instead of a backlog running later and answering many times.
    if(this.codex.ready===false&&!this.store.taskForMessage(message.tenant,message.message)){this.notice(message,UNAVAILABLE);return;}
    const resources=config.resources.filter(r=>r.regex.test(message.text)).map(r=>r.key);
    const result=this.store.create({tenant_key:message.tenant,user_id:message.user,chat_id:message.chat,source_message_id:message.message,
      instruction:message.text},resources,message.attachments);
    if(result.created)log.info({taskId:result.task.id},'任务已入队');
  }
  /** /new: forget the conversation. A task still running is stopped first, so this is also how a user gets going again when one hangs. */
  private async newConversation(message:IncomingMessage){
    const {tenant,user}=message,unfinished=this.store.unfinished(tenant,user);
    for(const task of unfinished){
      if(task.status!=='QUEUED'){
        this.store.update(task.id,{cancel_requested:1});
        if(this.interrupt(task))for(const until=Date.now()+this.stopWaitMs;this.runs.has(task.id)&&Date.now()<until;)await new Promise(r=>setTimeout(r,50));
      }
      // Ends it here if the process did not (a no-op for a task that has ended): the user's queue must not stay blocked.
      this.store.transition(task.id,task.status==='QUEUED'?'CANCELLED':'INTERRUPTED',{error:''});
    }
    this.notice(message,this.store.resetSession(tenant,user)||unfinished.length?'已开始新的对话，之前的内容不再带入。':'还没有对话，直接发送任务即可。');
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
    let turn:Turn|undefined;
    try{
      if(this.store.get(task.id)?.cancel_requested){this.store.transition(task.id,'CANCELLED');return;}
      const {session,folder}=await this.prepare(task);
      const current=this.store.get(task.id)!;
      if(current.cancel_requested||abort.signal.aborted||terminal(current.status)){if(!terminal(current.status))this.store.transition(task.id,this.stopping?'FAILED':'CANCELLED',this.stopping?{error:BOT_DOWN}:{});return;}
      const input=codexInput(current.instruction,this.store.inputs(task.id));
      // From here on the input has been handed over, and the task is never started a second time.
      this.store.event(task.id,'CODEX_SUBMITTED',{});
      turn=new Turn();
      await this.execute(this.store.get(task.id)!,session,folder,input,turn,abort.signal);
    }catch(e){
      const current=this.store.get(task.id);
      if(!current||terminal(current.status))return;
      if(abort.signal.aborted&&!this.stopping){
        // The user stopped it: what Codex had said by then stays on the card.
        await this.finish(current,turn,this.store.submitted(task.id)?'INTERRUPTED':'CANCELLED');
      }else{
        log.warn({taskId:task.id,error:safeError(e)},this.stopping?'服务停止，任务中断':'Codex 执行失败');
        await this.finish(current,turn,'FAILED',BOT_DOWN);
      }
    }
    finally{this.runs.delete(task.id);}
  }
  /** Runs one turn and ends the task by its outcome. */
  private async execute(task:Task,session:UserSession,folder:string,input:UserInput[],turn:Turn,signal:AbortSignal){
    let threadId=session.thread_id,received=false;
    for(let renewed=false;;){
      try{
        for await(const event of this.codex.run({threadId,cwd:folder,input,signal})){received=true;this.onEvent(task,session,turn,event);}
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
    if(changed&&turn.state==='running'&&this.store.get(task.id)!.progress!==turn.progress){
      this.store.update(task.id,{progress:turn.progress});this.store.enqueue(`view:${task.id}`,task.id,'VIEW',{});
    }
  }
  /** Ends the task. The images Codex generated during it are recorded first: the final transition is what delivers them. */
  private async finish(task:Task,turn:Turn|undefined,status:'COMPLETED'|'FAILED'|'INTERRUPTED'|'CANCELLED',error=''){
    const threadId=turn?.threadId||this.store.get(task.id)?.thread_id;
    if(threadId&&status!=='CANCELLED'){
      try{this.store.recordImages(task.id,await generatedImages(threadId,task.started_at??task.created_at));}
      catch(e){log.warn({taskId:task.id,error:safeError(e)},'读取生成的图片失败');}
    }
    this.store.transition(task.id,status,{result:turn?.answer??'',progress:turn?.progress??'',error});
    log.info({taskId:task.id,status},'任务结束');
  }
  /** Stops the task's Codex process; false when nothing is running it. */
  interrupt(task:Task){const run=this.runs.get(task.id);run?.abort();return !!run;}
  /** Service shutdown: every Codex process ends, and its task is finished as failed, so no card is left saying "running". */
  stop(){this.stopping=true;for(const run of this.runs.values())run.abort();}
}
