import {randomUUID} from 'node:crypto';
import path from 'node:path';
import {Store} from '../storage/database.js';
import {sessionInputs} from '../feishu/inputs.js';
import {NativeDesktop} from '../desktop/adapter.js';
import {taskWorkspace} from '../layout.js';
import {config} from '../config.js';
import {terminal,BOT_DOWN,type Task,type TaskView,type IncomingMessage,type UserSession} from '../types.js';
import {safeError,log} from '../log.js';
import {OutcomeUnknown,NotSubmitted,RpcRejected} from '../desktop/transport.js';
import {prepareInputs,type ResourceDownloader} from '../feishu/inputs.js';
import {authorized} from '../access.js';

export const HELP='直接发送任务；群聊需 @机器人。\n/tasks 列出自己的任务\n/status 任务ID\n/cancel 任务ID（无响应时再发一次强制结束）\n/approve 任务ID 请求ID\n/deny 任务ID 请求ID\n/answer 任务ID 请求ID 回答或JSON\n/whoami 查看自己的 Open ID';
export const UNAVAILABLE=BOT_DOWN;
// Clock skew allowance for the start check, and how late a message may arrive: live ones take a second or two, Feishu's redeliveries of what was missed while the service was down take much longer.
const STALE_GRACE_MS=1000,MAX_DELIVERY_LAG_MS=30000;
export class TaskService {
  private startedAt=Date.now();
  /** Feishu's clock minus this machine's (ms), kept current by the main loop; message times are Feishu's. */
  clockOffsetMs=0;
  private interruptSent=new Set<string>();
  private starting=new Set<string>();
  constructor(public store:Store,public desktop:NativeDesktop,private downloader?:ResourceDownloader){
    desktop.on('state',(id:string)=>{try{this.onState(id);}catch(e){log.error({taskThread:id,error:safeError(e)},'Desktop 状态处理失败');}});
    desktop.on('online',()=>this.interruptSent.clear());
  }
  notice(message:IncomingMessage,text:string){this.store.enqueue(`notice:${message.tenant}:${message.message}`,null,'NOTICE',{chat:message.chat,message:message.message,text});}
  authorized(message:IncomingMessage){return authorized(message);}
  /** Marks a task as awaiting reconciliation once; repeating it on every pass would rewrite its events and card forever. */
  private markUnknown(id:string){const task=this.store.get(id);if(task&&(task.status!=='UNKNOWN'||task.error!==BOT_DOWN))this.store.transition(id,'UNKNOWN',{error:BOT_DOWN});}
  private async refreshSessionName(session:UserSession){
    if(!this.downloader?.getUserName)return;
    try{const name=await this.downloader.getUserName(session.user_id);if(name)this.store.nameSession(session.id,name);}
    catch(e){log.warn({error:safeError(e)},'用户名称查询失败，会话和任务继续使用现有名称');}
  }
  async syncSessionNames(){
    for(const session of this.store.sessions()){
      if(!this.desktop.ready)break;
      await this.refreshSessionName(session);
      await this.desktop.syncSessionTitle?.(session.id);
    }
  }
  async receive(message:IncomingMessage){
    // Feishu may redeliver events that arrived while this service was not running; those are stale and must not run now.
    const lagMs=Date.now()+this.clockOffsetMs-(message.createdAt??0);
    if(message.createdAt&&(message.createdAt<this.startedAt+this.clockOffsetMs-STALE_GRACE_MS||lagMs>MAX_DELIVERY_LAG_MS)){log.info({message:message.message,lagMs},'忽略服务不可用期间发送、事后才送达的消息');return;}
    if(message.text.trim()==='/whoami'){this.notice(message,`你的 Open ID：${message.user}\n租户：${message.tenant}`);return;}
    if(!this.authorized(message)){this.notice(message,config.tenants.length?'当前企业或会话尚未开放机器人使用权限，请联系管理员。':`尚未获授权。请让管理员将你的 Open ID 加入 ALLOWED_USERS：${message.user}`);return;}
    if(message.error){this.notice(message,message.error);return;}
    if(message.text.trimStart().startsWith('/')&&!message.attachments?.length){await this.command(message);return;}
    if(!message.text.trim()&&!message.attachments?.length)return;
    // One input runs at a time. Anything sent while the user's previous input is still unfinished gets no result back, so it is not an input at all:
    // it is dropped without a reply and never queued. (A redelivered event of the message already running lands here too and is equally harmless.)
    if(this.store.busy(message.tenant,message.user)){log.info({message:message.message,user:message.user},'忽略执行期间收到的输入');return;}
    // Nothing is queued while Desktop is down: the user is told at once and sends it again, instead of a backlog running later and answering many times.
    if(this.desktop.ready===false&&!this.store.taskForMessage(message.tenant,message.message)){this.notice(message,UNAVAILABLE);return;}
    const resources=config.resources.filter(r=>r.regex.test(message.text)).map(r=>r.key);
    const result=this.store.create({tenant_key:message.tenant,user_id:message.user,chat_id:message.chat,source_message_id:message.message,
      instruction:message.text},resources,message.attachments);
    if(result.created)log.info({taskId:result.task.id},'任务已入队');
  }
  private owned(message:IncomingMessage,id:string){
    const t=this.store.get(id);
    if(!t||t.tenant_key!==message.tenant||(t.user_id!==message.user&&!config.admins.includes(message.user)))throw Error('任务不存在或无权操作');
    return t;
  }
  private async command(message:IncomingMessage){
    const [command,id,requestId,...rest]=message.text.trim().split(/\s+/);
    try{
      if(command==='/help'){this.notice(message,HELP);return;}
      if(command==='/tasks'){
        const tasks=this.store.recent(message.tenant,message.user,10);
        this.notice(message,tasks.length?tasks.map(t=>`${t.id} ${t.status}`).join('\n'):'暂无任务');return;
      }
      if(!['/status','/cancel','/approve','/deny','/answer'].includes(command)){this.notice(message,HELP);return;}
      const task=this.owned(message,id);
      if(command==='/status'){this.notice(message,`任务：${task.id}\n状态：${task.status}\n${task.error||task.result||task.progress||'等待执行'}`.slice(0,15000));return;}
      if(command==='/cancel'){
        if(task.status==='QUEUED')this.store.transition(task.id,'CANCELLED');
        else if(task.status==='UNKNOWN'&&!task.turn_id&&!this.starting.has(task.id)){
          // Desktop never confirmed this task, so nothing can resolve it later. Release the user's queue instead of waiting forever.
          this.store.transition(task.id,'CANCELLED',{error:''});
          this.notice(message,this.store.submission(task.id)?'任务已取消。如果 Desktop 已收到该请求，它仍可能继续执行。':'任务已取消。');return;
        }
        else if(!terminal(task.status)&&task.cancel_requested){
          // Asked before and Desktop has still not confirmed the interrupt (it may have crashed mid-turn). Waiting longer helps nobody and
          // would keep the user's queue blocked for good, so the task ends here; Desktop is told once more, best effort.
          this.store.transition(task.id,'CANCELLED',{error:'',progress:''});
          if(task.thread_id&&task.turn_id)void this.interrupt(task).catch(e=>log.warn({taskId:task.id,error:safeError(e)},'强制结束后中断仍未确认'));
          this.notice(message,'已强制结束任务。如果 Desktop 仍在执行，它可能继续运行，结果不会再回传。');return;
        }
        else if(!terminal(task.status)){
          this.store.update(task.id,{cancel_requested:1});
          // An interrupt that cannot be confirmed now is retried on the next state update; the user is not told the bot is down for it.
          if(task.thread_id&&task.turn_id)await this.interrupt(task).catch(e=>log.warn({taskId:task.id,error:safeError(e)},'中断尚未确认'));
        }
        this.notice(message,terminal(task.status)||task.status==='QUEUED'?'任务已结束或已取消。':'已请求中断，等待 Desktop 确认。若长时间没有结果，再发一次 /cancel 可强制结束。');return;
      }
      if(terminal(task.status))throw Error('任务已经结束');
      const row=this.store.db.prepare("SELECT * FROM pending_requests WHERE task_id=? AND request_id=? AND status='PENDING'").get(task.id,requestId) as any;
      if(!row||row.expires_at<Date.now())throw Error('交互请求不存在、已处理或已过期');
      if(row.kind==='input'&&command!=='/answer')throw Error('此请求需要回答，请使用 /answer；也可用 /cancel 中断任务');
      if(!task.thread_id||!task.turn_id)throw Error('Desktop 执行关联尚未确认');
      // Persist before sending. A repeated Feishu event must not repeat an approval write.
      const claimed=this.store.db.prepare("UPDATE pending_requests SET status='RESPONDING' WHERE task_id=? AND request_id=? AND status='PENDING'").run(task.id,requestId);
      if(!claimed.changes)throw Error('该请求正在处理中');
      try{
        await this.desktop.respond(task.thread_id,task.turn_id,{id:row.request_id,kind:row.kind,summary:row.summary,payload:JSON.parse(row.payload)},command==='/approve'?'accept':command==='/deny'?'decline':rest.join(' '));
        this.store.db.prepare("UPDATE pending_requests SET status='RESOLVED' WHERE task_id=? AND request_id=?").run(task.id,requestId);
        this.notice(message,'响应已提交，等待 Desktop 继续执行。');
      }catch(e){
        // Validation errors are safe to fix; network uncertainty remains RESPONDING until snapshot reconciliation.
        if(!(e instanceof OutcomeUnknown))this.store.db.prepare("UPDATE pending_requests SET status='PENDING' WHERE task_id=? AND request_id=?").run(task.id,requestId);
        throw e;
      }
    }catch(e){
      const desktopDown=e instanceof OutcomeUnknown||e instanceof NotSubmitted||e instanceof RpcRejected||/^Desktop/.test(safeError(e));
      if(desktopDown)log.warn({error:safeError(e)},'命令无法送达 Desktop');
      this.notice(message,desktopDown?BOT_DOWN:safeError(e));
    }
  }
  async start(task:Task){
    if(this.starting.has(task.id))return;this.starting.add(task.id);
    let submitting=false;
    try{
      if(this.store.get(task.id)?.cancel_requested){this.store.transition(task.id,'CANCELLED');return;}
      if(this.store.inputs(task.id).length){
        if(!this.downloader)throw Error('附件下载服务不可用');
        this.store.update(task.id,{progress:'正在接收附件'});this.store.enqueue(`view:${task.id}`,task.id,'VIEW',{});
        const owner=this.store.sessionFor(task);await this.refreshSessionName(owner);
        // Attachments are saved in Codex's own folder for this chat, so the chat has to exist first.
        const root=(await this.desktop.prepareSession(task)).workspace_path!;
        await prepareInputs(this.store,task,this.downloader,sessionInputs(root));
      }
      const current=this.store.get(task.id)!;
      if(current.cancel_requested||terminal(current.status)){if(!terminal(current.status))this.store.transition(task.id,'CANCELLED');return;}
      const session=this.store.sessionFor(task);
      await this.refreshSessionName(session);
      this.store.event(task.id,'DESKTOP_SUBMITTING',{});submitting=true;
      const created=await this.desktop.startTask(this.store.get(task.id)!);
      this.store.update(task.id,{thread_id:created.threadId,workspace_path:created.workspace});
      await this.desktop.follow(created.threadId);
    }catch(e){
      const current=this.store.get(task.id);
      if(e instanceof NotSubmitted){
        // Nothing reached Desktop. Cancel if asked to; otherwise fail now rather than run later, when the user has probably sent it again.
        if(current?.cancel_requested)this.store.transition(task.id,'CANCELLED');
        else this.store.transition(task.id,'FAILED',{error:UNAVAILABLE,progress:''});
      }else if(e instanceof RpcRejected&&this.store.submission(task.id)&&!current?.turn_id){
        // Desktop explicitly refused the business request, so it is certain nothing is running.
        log.warn({taskId:task.id,error:safeError(e)},'Desktop 拒绝了请求，任务未执行');
        this.store.transition(task.id,'FAILED',{error:BOT_DOWN});
      }else{
        // Only a persisted send attempt or a lost session creation makes the outcome uncertain; any other failure happened before anything reached Desktop.
        const uncertain=submitting&&(!!this.store.submission(task.id)||e instanceof OutcomeUnknown);
        log.warn({taskId:task.id,uncertain,error:safeError(e)},uncertain?'提交结果不明，需要核对 Desktop':'任务未提交给 Codex');
        this.store.transition(task.id,uncertain?'UNKNOWN':'FAILED',{error:BOT_DOWN});
      }
    }
    finally{this.starting.delete(task.id);}
  }
  onState(threadId:string){
    const task=this.store.byThread(threadId);if(!task||terminal(task.status))return;
    const cached=this.desktop.stream.states.get(threadId);
    const submission=this.store.submission(task.id);
    const view=this.desktop.stream.view(threadId,task.turn_id,submission?.clientUserMessageId);
    if(!view)return;
    if(!task.turn_id){
      const session=task.session_id?this.store.session(task.session_id):undefined;
      const root=session?.workspace_path??cached?.state.projectlessOutputDirectory??cached?.state.cwd;
      if(session&&root)this.store.bindSession(session.id,threadId,root);
      this.store.update(task.id,{turn_id:view.turnId,workspace_path:task.workspace_path??(session&&root?taskWorkspace(root):root)});
    }
    const progress=view.commentary;
    // Persist images before the terminal transition can release the Desktop subscription.
    const imagesChanged=this.store.recordImages(task.id,view.images);
    const patch={result:view.answer,error:view.error,progress,last_revision:view.revision};
    const completed=({completed:'COMPLETED',failed:'FAILED',interrupted:'INTERRUPTED'} as const)[view.status as 'completed'];
    if(completed){this.store.transition(task.id,completed,patch);this.store.meta('desktop_caller',{threadId,turnId:view.turnId});setImmediate(()=>{if(!this.store.byThread(threadId))this.desktop.unfollow(threadId);});log.info({taskId:task.id,status:completed},'任务结束');return;}
    const previousRequests=JSON.stringify(this.store.pending(task.id));this.store.setRequests(task.id,view.requests);
    const status=view.requests.length?'WAITING_APPROVAL':'RUNNING';
    if(task.status!==status)this.store.transition(task.id,status,patch);
    else if(imagesChanged||task.result!==view.answer||task.progress!==progress||task.error!==view.error||previousRequests!==JSON.stringify(this.store.pending(task.id))){this.store.update(task.id,patch);this.store.enqueue(`view:${task.id}`,task.id,'VIEW',{});}
    else if(task.last_revision!==view.revision)this.store.update(task.id,{last_revision:view.revision});
    if(task.cancel_requested)void this.interrupt(this.store.get(task.id)!).catch(e=>log.warn({taskId:task.id,error:safeError(e)},'中断尚未确认'));
  }
  async interrupt(task:Task){
    if(!task.thread_id||!task.turn_id)return;
    const key=`${task.id}:${task.turn_id}`;if(this.interruptSent.has(key))return;
    this.interruptSent.add(key);
    // Forget a failed attempt so /cancel and the next state update can retry it.
    try{await this.desktop.interrupt(task.thread_id,task.turn_id);}catch(e){this.interruptSent.delete(key);throw e;}
  }
  async reconcile(){
    // Nothing of this task reached Desktop. It is not run later, when the user has probably moved on or sent it again: it ends now, and the user is told once.
    const voidTask=(id:string)=>this.store.transition(id,this.store.get(id)?.cancel_requested?'CANCELLED':'FAILED',{error:this.store.get(id)?.cancel_requested?'':BOT_DOWN,progress:''});
    for(const task of this.store.active()){
      if(this.starting.has(task.id))continue;
      try{
        const session=task.session_id?this.store.session(task.session_id):undefined;
        // Only a persisted send attempt makes the outcome uncertain. Without one the business input never left,
        // so queue it again before following an old thread; startTask can then replace an archived or missing session.
        if(!this.store.submission(task.id)&&!task.turn_id&&session?.thread_id&&session.workspace_path){voidTask(task.id);continue;}
        if(task.thread_id)await this.desktop.follow(task.thread_id);
        else if(this.store.inputs(task.id).length&&!this.store.db.prepare("SELECT 1 FROM task_events WHERE task_id=? AND kind='DESKTOP_SUBMITTING' LIMIT 1").get(task.id))voidTask(task.id);
        else{this.markUnknown(task.id);await this.desktop.recoverUnbound(task);}
        // recoverUnbound may have bound the session after a lost creation response; an input that never went out is void like any other.
        const current=this.store.get(task.id)!;
        if(!terminal(current.status)&&!this.store.submission(task.id)&&!current.turn_id&&current.thread_id&&current.session_id&&this.store.session(current.session_id)?.workspace_path)voidTask(task.id);
      }catch(e){log.warn({taskId:task.id,error:safeError(e)},'核对 Desktop 失败');this.markUnknown(task.id);}
    }
  }
}
