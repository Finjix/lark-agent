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

export const HELP='直接发送任务；群聊需 @机器人。\n/key 你的API Key 设置自己的 Key（请私聊发送）\n/new 忘掉之前的对话，重新开始\n/tasks 列出自己的任务\n/status 任务ID\n/cancel 任务ID（无响应时再发一次强制结束）\n/whoami 查看自己的 Open ID';
export const UNAVAILABLE=BOT_DOWN;
export const KEY_NEEDED='使用前请先私聊机器人发送 /key 你的API Key，设置自己的 OpenAI API Key。';
export const KEY_REJECTED='你的 API Key 被拒绝了（可能填错或已失效），请私聊机器人发送 /key 你的API Key 重新设置。';
// What Codex says when the user's key is not accepted.
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
    if(message.text.trim()==='/whoami'){this.notice(message,`你的 Open ID：${message.user}\n租户：${message.tenant}`);return;}
    if(!this.authorized(message)){this.notice(message,config.tenants.length?'当前企业或会话尚未开放机器人使用权限，请联系管理员。':`尚未获授权。请让管理员将你的 Open ID 加入 ALLOWED_USERS：${message.user}`);return;}
    if(message.error){this.notice(message,message.error);return;}
    if(message.text.trimStart().startsWith('/')&&!message.attachments?.length){await this.command(message);return;}
    if(!message.text.trim()&&!message.attachments?.length)return;
    // Codex runs on the user's own API key; until they set one nothing is queued.
    if(!this.store.apiKey(message.tenant,message.user)){this.notice(message,KEY_NEEDED);return;}
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
  private owned(message:IncomingMessage,id:string){
    const t=this.store.get(id);
    if(!t||t.tenant_key!==message.tenant||(t.user_id!==message.user&&!config.admins.includes(message.user)))throw Error('任务不存在或无权操作');
    return t;
  }
  private async command(message:IncomingMessage){
    const [command,id]=message.text.trim().split(/\s+/);
    try{
      if(command==='/help'){this.notice(message,HELP);return;}
      if(command==='/tasks'){
        const tasks=this.store.recent(message.tenant,message.user,10);
        this.notice(message,tasks.length?tasks.map(t=>`${t.id} ${t.status}`).join('\n'):'暂无任务');return;
      }
      if(command==='/key'){this.notice(message,this.setKey(message,id));return;}
      if(command==='/new'){
        if(this.store.busy(message.tenant,message.user))throw Error('有任务正在执行，请等它结束，或先 /cancel');
        this.notice(message,this.store.resetSession(message.tenant,message.user)?'已开始新的对话，之前的内容不再带入。':'还没有对话，直接发送任务即可。');return;
      }
      if(!['/status','/cancel'].includes(command)){this.notice(message,HELP);return;}
      const task=this.owned(message,id);
      if(command==='/status'){this.notice(message,`任务：${task.id}\n状态：${task.status}\n${task.error||task.result||task.progress||'等待执行'}`.slice(0,15000));return;}
      if(terminal(task.status)||task.status==='QUEUED'){
        if(task.status==='QUEUED')this.store.transition(task.id,'CANCELLED');
        this.notice(message,'任务已结束或已取消。');return;
      }
      if(task.cancel_requested){
        // Asked before and the Codex process is still there. Waiting longer helps nobody and would keep the user's queue blocked, so the task ends here.
        this.store.transition(task.id,'CANCELLED',{error:'',progress:''});this.interrupt(task);
        this.notice(message,'已强制结束任务。如果 Codex 仍在执行，它可能继续运行，结果不会再回传。');return;
      }
      this.store.update(task.id,{cancel_requested:1});
      // A task nothing is running (a leftover) cannot answer, so it ends here.
      if(!this.interrupt(task)){this.store.transition(task.id,'CANCELLED',{error:''});this.notice(message,'任务已取消。');return;}
      this.notice(message,'已请求中断。若长时间没有结果，再发一次 /cancel 可强制结束。');
    }catch(e){this.notice(message,safeError(e));}
  }
  /** /key <API key>, /key (what is set), /key clear. The key itself is never repeated or logged. */
  private setKey(message:IncomingMessage,value?:string){
    const {tenant,user}=message,current=this.store.apiKey(tenant,user);
    if(!value)return current?`已设置 API Key：${current.slice(0,5)}...${current.slice(-4)}。发送 /key clear 清除，或 /key 新的Key 替换。`:KEY_NEEDED;
    if(value==='clear')return this.store.clearApiKey(tenant,user)?'已清除你的 API Key。':'你还没有设置 API Key。';
    // Everyone in a group can read what is sent there.
    if(message.isGroup)return '请私聊机器人发送 /key，群里的消息大家都看得到。如果已经在群里发过，请撤回消息并到 OpenAI 后台作废这个 Key。';
    if(!/^[\x21-\x7e]{16,}$/.test(value))return 'API Key 的格式不对：应是一整串字符（不含空格和中文）。';
    this.store.setApiKey(tenant,user,value);
    return '已保存你的 API Key，之后的任务都用它。为安全起见，建议撤回刚才这条消息。';
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
        for await(const event of this.codex.run({threadId,cwd:folder,input,apiKey:this.store.apiKey(task.tenant_key,task.user_id),signal})){received=true;this.onEvent(task,session,turn,event);}
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
    await this.finish(this.store.get(task.id)!,turn,turn.state==='completed'?'COMPLETED':'FAILED',AUTH_FAILURE.test(turn.error)?KEY_REJECTED:turn.error);
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
