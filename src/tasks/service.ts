import {randomUUID} from 'node:crypto';
import {Store} from '../storage/database.js';
import {sessionInputs} from '../feishu/inputs.js';
import {NativeDesktop,taskWorkspace,sessionDirectory} from '../desktop/adapter.js';
import {config} from '../config.js';
import {terminal,type Task,type TaskView,type IncomingMessage,type UserSession} from '../types.js';
import {safeError,log} from '../log.js';
import {OutcomeUnknown} from '../desktop/transport.js';
import {prepareInputs,type ResourceDownloader} from '../feishu/inputs.js';
import {authorized} from '../access.js';

export const HELP='直接发送任务；群聊需 @机器人。\n/tasks 列出自己的任务\n/status 任务ID\n/cancel 任务ID\n/approve 任务ID 请求ID\n/deny 任务ID 请求ID\n/answer 任务ID 请求ID 回答或JSON\n/whoami 查看自己的 Open ID';
export class TaskService {
  private interruptSent=new Set<string>();
  private starting=new Set<string>();
  constructor(public store:Store,public desktop:NativeDesktop,private downloader?:ResourceDownloader){
    desktop.on('state',(id:string)=>{try{this.onState(id);}catch(e){log.error({taskThread:id,error:safeError(e)},'Desktop 状态处理失败');}});
    desktop.on('online',()=>this.interruptSent.clear());
  }
  notice(message:IncomingMessage,text:string){this.store.enqueue(`notice:${message.tenant}:${message.message}`,null,'NOTICE',{chat:message.chat,message:message.message,text});}
  authorized(message:IncomingMessage){return authorized(message);}
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
    if(message.text.trim()==='/whoami'){this.notice(message,`你的 Open ID：${message.user}\n租户：${message.tenant}`);return;}
    if(!this.authorized(message)){this.notice(message,config.tenants.length?'当前企业或会话尚未开放机器人使用权限，请联系管理员。':`尚未获授权。请让管理员将你的 Open ID 加入 ALLOWED_USERS：${message.user}`);return;}
    if(message.error){this.notice(message,message.error);return;}
    if(message.text.trimStart().startsWith('/')&&!message.attachments?.length){await this.command(message);return;}
    if(!message.text.trim()&&!message.attachments?.length)return;
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
        const tasks=this.store.list().filter(t=>t.tenant_key===message.tenant&&t.user_id===message.user).slice(-10).reverse();
        this.notice(message,tasks.length?tasks.map(t=>`${t.id} ${t.status}`).join('\n'):'暂无任务');return;
      }
      if(!['/status','/cancel','/approve','/deny','/answer'].includes(command)){this.notice(message,HELP);return;}
      const task=this.owned(message,id);
      if(command==='/status'){this.notice(message,`任务：${task.id}\n状态：${task.status}\n${task.error||task.result||task.progress||'等待执行'}`.slice(0,15000));return;}
      if(command==='/cancel'){
        if(task.status==='QUEUED')this.store.transition(task.id,'CANCELLED');
        else if(!terminal(task.status)){
          this.store.update(task.id,{cancel_requested:1});
          if(task.thread_id&&task.turn_id)await this.interrupt(task);
        }
        this.notice(message,terminal(task.status)||task.status==='QUEUED'?'任务已结束或已取消。':'已请求中断，等待 Desktop 确认。');return;
      }
      if(terminal(task.status))throw Error('任务已经结束');
      const row=this.store.db.prepare("SELECT * FROM pending_requests WHERE task_id=? AND request_id=? AND status='PENDING'").get(task.id,requestId) as any;
      if(!row||row.expires_at<Date.now())throw Error('交互请求不存在、已处理或已过期');
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
    }catch(e){this.notice(message,safeError(e));}
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
        const root=this.store.reserveWorkspace(owner.id,sessionDirectory(this.store.session(owner.id)!));
        await prepareInputs(this.store,task,this.downloader,sessionInputs(root));
      }
      const current=this.store.get(task.id)!;
      if(current.cancel_requested||terminal(current.status)){if(!terminal(current.status))this.store.transition(task.id,'CANCELLED');return;}
      const session=this.store.sessionFor(task);
      await this.refreshSessionName(session);
      if(session.thread_id&&session.workspace_path)this.store.update(task.id,{thread_id:session.thread_id,workspace_path:taskWorkspace(session.workspace_path,task.id)});
      this.store.event(task.id,'DESKTOP_SUBMITTING',{});submitting=true;
      const created=await this.desktop.startTask(this.store.get(task.id)!);
      this.store.update(task.id,{thread_id:created.threadId,workspace_path:created.workspace});
      await this.desktop.follow(created.threadId);
    }catch(e){this.store.transition(task.id,submitting?'UNKNOWN':'FAILED',{error:submitting?`需要核对 Desktop：${safeError(e)}`:`附件接收失败，任务未提交给 Codex：${safeError(e)}`});}
    finally{this.starting.delete(task.id);}
  }
  onState(threadId:string){
    const task=this.store.byThread(threadId);if(!task||terminal(task.status))return;
    const cached=this.desktop.stream.states.get(threadId);
    const submission=this.store.submission(task.id);
    const view=this.desktop.stream.view(threadId,task.turn_id,submission?undefined:`[LARK_TASK_ID:${task.submission_key}]`,submission?.clientUserMessageId);
    if(!view)return;
    if(!task.turn_id){
      const session=task.session_id?this.store.session(task.session_id):undefined;
      const root=session?session.workspace_path??sessionDirectory(session):cached?.state.projectlessOutputDirectory??cached?.state.cwd;
      if(session&&root)this.store.bindSession(session.id,threadId,root);
      this.store.update(task.id,{turn_id:view.turnId,workspace_path:task.workspace_path??(session&&root?taskWorkspace(root,task.id):root)});
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
    this.interruptSent.add(key);await this.desktop.interrupt(task.thread_id,task.turn_id);
  }
  async reconcile(){
    for(const task of this.store.active()){
      if(this.starting.has(task.id))continue;
      try{
        // Preparation never sent the business input. Requeue before following an old
        // thread so startTask can replace an archived or missing session safely.
        if(this.store.eventPayload(task.id,'DESKTOP_RAW_REQUEST')&&!this.store.submission(task.id)&&!task.turn_id&&
          task.session_id&&this.store.session(task.session_id)?.thread_id&&this.store.session(task.session_id)?.workspace_path){
          this.store.transition(task.id,'QUEUED',{error:'',progress:'等待提交用户原文',started_at:null});
          continue;
        }
        if(task.thread_id)await this.desktop.follow(task.thread_id);
        else if(this.store.inputs(task.id).length&&!this.store.db.prepare("SELECT 1 FROM task_events WHERE task_id=? AND kind='DESKTOP_SUBMITTING' LIMIT 1").get(task.id))this.store.transition(task.id,'QUEUED',{error:'',progress:'等待恢复附件接收',started_at:null});
        else{this.store.transition(task.id,'UNKNOWN',{error:'服务重启或提交结果不明，正在核对'});await this.desktop.recoverUnbound(task);}
        // Initialization is separate from the business turn. Only preparation with no
        // persisted send attempt can be queued again; uncertain raw writes stay UNKNOWN.
        const current=this.store.get(task.id)!;
        if(!terminal(current.status)&&this.store.eventPayload(task.id,'DESKTOP_RAW_REQUEST')&&!this.store.submission(task.id)&&
          current.thread_id&&current.session_id&&this.store.session(current.session_id)?.workspace_path){
          this.store.transition(task.id,'QUEUED',{error:'',progress:'等待提交用户原文',started_at:null});
        }
      }catch(e){this.store.transition(task.id,'UNKNOWN',{error:safeError(e)});}
    }
  }
}
