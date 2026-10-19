import fs from 'node:fs/promises';
import path from 'node:path';
import {createHash,randomUUID} from 'node:crypto';
import {Store} from '../storage/database.js';
import {config} from '../config.js';
import {terminal,type Task} from '../types.js';
import {FeishuClient,FeishuError} from './client.js';
import {cardJson,renderTask,messageSummary,working,elapsedSeconds} from './cards.js';
import {safeError,log} from '../log.js';
import {stageGeneratedImage} from './images.js';

/** Runs calls one at a time, at most `cap` starts per sliding `interval` ms. */
export class RateGate {
  private tail:Promise<unknown>=Promise.resolve(); private starts:number[]=[];
  constructor(private cap:number,private interval:number){}
  add<T>(fn:()=>Promise<T>):Promise<T>{
    const run=this.tail.then(async()=>{
      for(;;){
        const now=Date.now();this.starts=this.starts.filter(t=>now-t<this.interval);
        if(this.starts.length<this.cap)break;
        await new Promise(r=>setTimeout(r,this.starts[0]+this.interval-now));
      }
      this.starts.push(Date.now());return fn();
    });
    this.tail=run.catch(()=>{});return run;
  }
  onIdle(){return this.tail;}
}
export async function ownedArtifact(workspace:string,candidate:string){
  const root=await fs.realpath(workspace),file=await fs.realpath(candidate),relative=path.relative(root,file);
  if(!relative||relative.startsWith('..'+path.sep)||relative==='..'||path.isAbsolute(relative))throw Error('交付文件不属于当前任务目录');
  const stat=await fs.stat(file);if(!stat.isFile())throw Error('交付项不是文件');
  if(stat.size>30*1024*1024)throw Error('文件超过当前机器人 30 MB 上传上限');
  return {path:file,name:path.basename(file),size:stat.size};
}
/**
 * Files Codex put in its working folder while this task ran: new or changed between its start and end (a file copied there counts
 * by its creation time). No dotfiles or symlinks, a few levels deep, capped so a stray tree cannot flood the chat.
 */
// The attachments the user sent live in the workspace's input/ and are not Codex's output.
const MAX_UNLINKED=10,SLACK_MS=2000,INPUT_DIR='input';
export async function outputFiles(root:string,window?:{from:number;to:number}){
  const found:string[]=[];
  const walk=async(directory:string,depth:number):Promise<void>=>{
    let entries;try{entries=await fs.readdir(directory,{withFileTypes:true});}catch{return;}
    for(const entry of entries.sort((a,b)=>a.name.localeCompare(b.name))){
      if(found.length>=MAX_UNLINKED)return;
      if(entry.name.startsWith('.'))continue;
      const full=path.join(directory,entry.name);
      if(entry.isFile()){
        if(window){const stat=await fs.stat(full),at=Math.max(stat.mtimeMs,stat.birthtimeMs);if(at<window.from-SLACK_MS||at>window.to+SLACK_MS)continue;}
        found.push(full);
      }else if(entry.isDirectory()&&depth<3&&!(depth===0&&entry.name===INPUT_DIR))await walk(full,depth+1);
    }
  };
  await walk(root,0);return found;
}
/** Local file targets of the markdown links in a reply. Parentheses inside a path ("report (2).pdf") are balanced, not taken for the end of the link. */
export function artifactLinks(text:string){
  const links:string[]=[];
  for(const open of text.matchAll(/\]\(/g)){
    const start=open.index!+2;let target:string;
    if(text[start]==='<'){const end=text.indexOf('>',start);if(end<0||text[end+1]!==')')continue;target=text.slice(start+1,end);}
    else{let depth=0,end=start;for(;end<text.length&&text[end]!=='\n';end++){if(text[end]==='(')depth++;else if(text[end]===')'&&!depth--)break;}if(text[end]!==')')continue;target=text.slice(start,end);}
    if(target.includes('\n')||!/^(?:[A-Za-z]:[\\/]|\/)/.test(target))continue;
    try{links.push(decodeURIComponent(target).replace(/:\d+$/,''));}catch{links.push(target);}
  }
  return links;
}
/** True when the path lies, as written, inside the folder. Links elsewhere (the user's own attachments, site paths like /docs/x) are not deliverables. */
export async function lexicallyInside(workspace:string,candidate:string){
  const roots=[path.resolve(workspace),await fs.realpath(workspace).catch(()=>'')].filter(Boolean);
  return roots.some(root=>{const relative=path.relative(root,path.resolve(candidate));return !!relative&&relative!=='..'&&!relative.startsWith('..'+path.sep)&&!path.isAbsolute(relative);});
}
export class Delivery {
  private timer?:NodeJS.Timeout;private busy=false;private filesBusy=false;private stopping=false;
  private renderedSeconds=new Map<string,number>();
  private gate=new RateGate(5,1000);
  // Uploads wait on their own gate and loop: a large file on a slow link must not hold up everyone's streaming cards.
  private fileGate=new RateGate(2,1000);
  constructor(private store:Store,private feishu:FeishuClient){}
  start(){this.timer=setInterval(()=>{void this.tick();},config.CARD_UPDATE_INTERVAL_MS);void this.tick();}
  async tick(){
    // Replies go out over REST, so a reconnecting long connection must not hold them back; only wait until the client has started.
    if(this.stopping||this.feishu.started===false)return;
    const files=this.pumpFiles();
    if(this.busy){await files;return;}this.busy=true;
    try{
      // Refresh the timer even when Codex has no new text. Preserve retry backoff and failures.
      for(const task of this.store.active())if(working(task)&&this.renderedSeconds.get(task.id)!==elapsedSeconds(task)){
        const row=this.store.db.prepare('SELECT status FROM delivery_outbox WHERE id=?').get(`view:${task.id}`) as {status:string}|undefined;
        if(row?.status==='DELIVERED')this.store.enqueue(`view:${task.id}`,task.id,'VIEW',{});
      }
      for(const row of this.store.due(false))await this.deliverRow(row);
    }catch(e){log.error({error:safeError(e)},'投递轮询失败，下一轮重试');}
    finally{this.busy=false;}
    await files;
  }
  private async pumpFiles(){
    if(this.filesBusy)return;this.filesBusy=true;
    try{for(const row of this.store.due(true))await this.deliverRow(row);}
    catch(e){log.error({error:safeError(e)},'文件投递轮询失败，下一轮重试');}
    finally{this.filesBusy=false;}
  }
  private async deliverRow(row:any){
    try{await this.deliver(row);this.store.ack(row.id,row.version);}
    // A file that is gone (cleaned up after the retention period, or removed by someone) cannot be uploaded however often it is retried.
    catch(e){const error=safeError(e);const permanent=(e instanceof FeishuError&&[99991672,99991663,99991664,230006,230002].includes(e.code))||(e as NodeJS.ErrnoException)?.code==='ENOENT';
      this.store.retry(row.id,row.version,error,permanent);log.warn({deliveryId:row.id,code:e instanceof FeishuError?e.code:undefined},'消息投递等待重试或配置修复');}
  }
  private async api<T>(fn:()=>Promise<T>,gate=this.gate):Promise<T>{return await gate.add(fn);}
  private async deliver(row:any){
    const payload=JSON.parse(row.payload);
    if(row.kind==='NOTICE'){
      await this.api(()=>this.feishu.reply(payload.message,payload.chat,'text',{text:payload.text},this.uuid(row.id)));return;
    }
    const task=this.store.get(row.task_id);if(!task)throw Error('投递任务不存在');
    if(row.kind==='FILE'){
      const artifact=this.store.db.prepare('SELECT * FROM task_artifacts WHERE id=?').get(payload.artifactId) as any;
      if(!artifact||artifact.message_id)return;
      // Revalidate realpath immediately before uploading; do not follow replaced symlinks outside the task directory.
      await ownedArtifact(task.workspace_path!,artifact.path);
      const image=artifact.kind==='image';
      if(!artifact.file_key){const uploaded=await this.api(()=>image?this.feishu.uploadImage(artifact.path):this.feishu.upload(artifact.path,artifact.name),this.fileGate);artifact.file_key=image?uploaded.image_key:uploaded.file_key;
        if(!artifact.file_key)throw Error('飞书未返回附件上传标识');
        this.store.db.prepare('UPDATE task_artifacts SET file_key=? WHERE id=?').run(artifact.file_key,artifact.id);}
      const sent=await this.api(()=>this.feishu.reply(task.source_message_id,task.chat_id,image?'image':'file',image?{image_key:artifact.file_key}:{file_key:artifact.file_key},this.uuid(row.id)),this.fileGate);
      this.store.db.prepare('UPDATE task_artifacts SET message_id=? WHERE id=?').run(sent.message_id,artifact.id);return;
    }
    if(row.kind!=='VIEW')throw Error('未知投递类型');
    if(terminal(task.status))await this.collectArtifacts(task);
    await this.updateCard(task);
  }
  private uuid(id:string){return createHash('sha256').update(id).digest('hex').slice(0,32);}
  private async updateCard(task:Task){
    const now=Date.now();let message=this.store.messages(task.id);const content=renderTask(task)||(working(task)?' ':'');
    // Queued tasks stay quiet until execution starts.
    if(!content&&!working(task))return;
    if(!message.card_id){
      const card=await this.api(()=>this.feishu.raw('POST','/open-apis/cardkit/v1/cards',{type:'card_json',data:JSON.stringify(cardJson(task,content,true,now))}));
      if(!card.card_id)throw Error('飞书未返回 card_id');
      this.store.db.prepare('UPDATE task_messages SET card_id=? WHERE task_id=?').run(card.card_id,task.id);message=this.store.messages(task.id);
    }
    if(!message.message_id){
      const sent=await this.api(()=>this.feishu.reply(task.source_message_id,task.chat_id,'interactive',{type:'card',data:{card_id:message.card_id}},this.uuid(`card:${task.id}`)));
      this.store.db.prepare('UPDATE task_messages SET message_id=? WHERE task_id=?').run(sent.message_id,task.id);
    }
    // Feishu streaming sessions close after 10 minutes. Switch to full-card updates before that limit.
    const closeStream=terminal(task.status)||Date.now()-(task.started_at??task.created_at)>9*60*1000;
    if(!terminal(task.status)&&!message.finalized&&!closeStream){
      // The colored header is outside the text element, so refresh the full card with the stream open.
      const {sequence}=this.store.nextSequence(task.id);
      await this.api(()=>this.feishu.raw('PUT',`/open-apis/cardkit/v1/cards/${message.card_id}`,{card:{type:'card_json',data:JSON.stringify(cardJson(task,content,true,now))},sequence,uuid:`u_${task.id}_${sequence}`}));
    }else if(message.finalized){
      const {sequence}=this.store.nextSequence(task.id);
      await this.api(()=>this.feishu.raw('PUT',`/open-apis/cardkit/v1/cards/${message.card_id}`,{card:{type:'card_json',data:JSON.stringify(cardJson(task,content,false,now))},sequence,uuid:`u_${task.id}_${sequence}`}));
    }else{
      const {sequence}=this.store.nextSequence(task.id);
      try{await this.api(()=>this.feishu.raw('PUT',`/open-apis/cardkit/v1/cards/${message.card_id}`,{card:{type:'card_json',data:JSON.stringify(cardJson(task,content,true,now))},sequence,uuid:`c_${task.id}_${sequence}`}));}
      catch(e){if(e instanceof FeishuError&&/stream|流式/i.test(e.message)){this.store.db.prepare('UPDATE task_messages SET finalized=1 WHERE task_id=?').run(task.id);}throw e;}
      if(closeStream){
        const {sequence}=this.store.nextSequence(task.id);
        await this.api(()=>this.feishu.raw('PATCH',`/open-apis/cardkit/v1/cards/${message.card_id}/settings`,{settings:JSON.stringify({config:{streaming_mode:false,summary:{content:messageSummary(content)}}}),sequence,uuid:`s_${task.id}_${sequence}`}));
        this.store.db.prepare('UPDATE task_messages SET finalized=1 WHERE task_id=?').run(task.id);
        // Persist the final body and preview after closing the stream.
        const {sequence:next}=this.store.nextSequence(task.id);
        await this.api(()=>this.feishu.raw('PUT',`/open-apis/cardkit/v1/cards/${message.card_id}`,{card:{type:'card_json',data:JSON.stringify(cardJson(task,content,false))},sequence:next,uuid:`u_${task.id}_${next}`}));
      }
    }
    if(working(task))this.renderedSeconds.set(task.id,elapsedSeconds(task,now));else this.renderedSeconds.delete(task.id);
  }
  private async collectArtifacts(task:Task){
    // A file that cannot be staged or validated must never hold back the final reply: tell the user and carry on.
    const reject=(name:string,e:unknown,key=name)=>{
      const id=`artifact-rejected:${task.id}:${this.uuid(key)}`,reason=safeError(e);
      if(this.store.db.prepare('SELECT 1 FROM delivery_outbox WHERE id=?').get(id))return;
      this.store.event(task.id,'ARTIFACT_REJECTED',{name,reason});
      this.store.enqueue(id,task.id,'NOTICE',{chat:task.chat_id,message:task.source_message_id,text:`任务 ${task.id} 的附件未发送：${name}
${reason}`});
    };
    const images=this.store.images(task.id);
    if(!task.workspace_path){for(const image of images)reject(`codex-image-${image.id}.png`,Error('生成图片缺少交付目录'));return;}
    // A reply often shows a generated image by its path in Codex's image folder. That image is already delivered from its staged copy, so the link is not a second file.
    const sources=new Set(images.flatMap(i=>i.savedPath?[path.resolve(i.savedPath).toLowerCase()]:[]));
    const paths=new Map(artifactLinks(task.result).filter(p=>!sources.has(path.resolve(p).toLowerCase())).map(p=>[p,'file']));
    for(const image of images){
      const name=`codex-image-${image.id}.png`;
      try{
        const staged=this.store.db.prepare('SELECT path FROM task_artifacts WHERE task_id=? AND name=?').get(task.id,name) as {path:string}|undefined;
        paths.set(staged?.path??await stageGeneratedImage(task,image),'image');
      }catch(e){reject(name,e);}
    }
    if(Buffer.byteLength(task.result)>12000){
      const file=path.join(task.workspace_path,`lark-result-${task.id}.md`);
      try{
        await fs.mkdir(task.workspace_path,{recursive:true});
        try{await fs.writeFile(file,task.result,{flag:'wx'});}catch(e:any){if(e.code!=='EEXIST')throw e;}
        paths.set(file,'file');
      }catch(e){reject(path.basename(file),e);}
    }
    // The workspace is the delivery folder: whatever Codex created or changed there during the task goes out even if the reply forgot to link it.
    for(const file of await outputFiles(task.workspace_path,{from:task.started_at??task.created_at,to:task.finished_at??Date.now()}))if(!paths.has(file))paths.set(file,/\.(png|jpe?g|gif|webp)$/i.test(file)?'image':'file');
    for(const [candidate,requestedKind] of paths){
      try{
        // A reply links all sorts of things (the user's own attachment, a site path); only what is meant to be in the delivery folder can fail loudly.
        if(!await lexicallyInside(task.workspace_path,candidate)){log.info({taskId:task.id,name:path.basename(candidate)},'回复里的链接不在交付目录内，不作为附件发送');continue;}
        const file=await ownedArtifact(task.workspace_path,candidate),id=this.uuid(`${task.id}:${file.path}`);
        // Feishu's image upload limit is 10 MB; larger images remain downloadable files.
        const kind=requestedKind==='image'&&file.size<=10*1024*1024?'image':'file';
        this.store.db.transaction(()=>{this.store.db.prepare('INSERT OR IGNORE INTO task_artifacts(id,task_id,path,name,size,kind) VALUES(?,?,?,?,?,?)').run(id,task.id,file.path,file.name,file.size,kind);
          const artifact=this.store.db.prepare('SELECT message_id FROM task_artifacts WHERE id=?').get(id) as any;
          if(!artifact.message_id)this.store.enqueue(`file:${id}`,task.id,'FILE',{artifactId:id});})();
      }catch(e){reject(path.basename(candidate),e,candidate);}
    }
  }
  /** One more pass once the one that may be running is over (tick would just return while it is). */
  async flush(){while(this.busy||this.filesBusy)await new Promise(r=>setTimeout(r,50));await this.tick();}
  async stop(){this.stopping=true;clearInterval(this.timer);while(this.busy||this.filesBusy)await new Promise(r=>setTimeout(r,50));await this.gate.onIdle();await this.fileGate.onIdle();}
}
