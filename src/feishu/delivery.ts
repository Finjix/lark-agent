import fs from 'node:fs/promises';
import path from 'node:path';
import {createHash,randomUUID} from 'node:crypto';
import PQueue from 'p-queue';
import {Store} from '../storage/database.js';
import {config} from '../config.js';
import {terminal,type Task} from '../types.js';
import {FeishuClient,FeishuError} from './client.js';
import {cardJson,renderTask,labels} from './cards.js';
import {safeError,log} from '../log.js';

export async function ownedArtifact(workspace:string,candidate:string){
  const root=await fs.realpath(workspace),file=await fs.realpath(candidate),relative=path.relative(root,file);
  if(!relative||relative.startsWith('..'+path.sep)||relative==='..'||path.isAbsolute(relative))throw Error('交付文件不属于当前任务目录');
  const stat=await fs.stat(file);if(!stat.isFile())throw Error('交付项不是文件');
  if(stat.size>30*1024*1024)throw Error('文件超过当前机器人 30 MB 上传上限');
  return {path:file,name:path.basename(file),size:stat.size};
}
export function artifactLinks(text:string){
  return [...text.matchAll(/\]\(<?((?:[A-Za-z]:[\\/]|\/)[^\n)<>]*?)>?\)/g)].map(m=>{
    try{return decodeURIComponent(m[1]).replace(/:\d+$/,'');}catch{return m[1];}
  });
}
export class Delivery {
  private timer?:NodeJS.Timeout;private busy=false;private stopping=false;
  private gate=new PQueue({concurrency:1,intervalCap:5,interval:1000,strict:true});
  constructor(private store:Store,private feishu:FeishuClient){}
  start(){this.timer=setInterval(()=>{void this.tick();},config.CARD_UPDATE_INTERVAL_MS);void this.tick();}
  async tick(){
    if(this.busy||this.stopping||!this.feishu.connected)return;this.busy=true;
    try{for(const row of this.store.due()){
      try{await this.deliver(row);this.store.ack(row.id,row.version);}
      catch(e){const error=safeError(e);const permanent=e instanceof FeishuError&&[99991672,99991663,99991664,230006,230002].includes(e.code);
        this.store.retry(row.id,row.version,error,permanent);log.warn({deliveryId:row.id,code:e instanceof FeishuError?e.code:undefined},'消息投递等待重试或配置修复');}
    }}finally{this.busy=false;}
  }
  private async api<T>(fn:()=>Promise<T>):Promise<T>{return (await this.gate.add(fn)) as T;}
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
      if(!artifact.file_key){const uploaded=await this.api(()=>this.feishu.upload(artifact.path,artifact.name));artifact.file_key=uploaded.file_key;
        this.store.db.prepare('UPDATE task_artifacts SET file_key=? WHERE id=?').run(artifact.file_key,artifact.id);}
      const sent=await this.api(()=>this.feishu.reply(task.source_message_id,task.chat_id,'file',{file_key:artifact.file_key},this.uuid(row.id)));
      this.store.db.prepare('UPDATE task_artifacts SET message_id=? WHERE id=?').run(sent.message_id,artifact.id);return;
    }
    if(row.kind!=='VIEW')throw Error('未知投递类型');
    await this.updateCard(task);
    if(terminal(task.status))await this.collectArtifacts(task);
  }
  private uuid(id:string){return createHash('sha256').update(id).digest('hex').slice(0,32);}
  private async updateCard(task:Task){
    let message=this.store.messages(task.id);const content=renderTask(task,this.store.pending(task.id));
    if(!message.card_id){
      const card=await this.api(()=>this.feishu.raw('POST','/open-apis/cardkit/v1/cards',{type:'card_json',data:JSON.stringify(cardJson(task,content,true))}));
      if(!card.card_id)throw Error('飞书未返回 card_id');
      this.store.db.prepare('UPDATE task_messages SET card_id=? WHERE task_id=?').run(card.card_id,task.id);message=this.store.messages(task.id);
    }
    if(!message.message_id){
      const sent=await this.api(()=>this.feishu.reply(task.source_message_id,task.chat_id,'interactive',{type:'card',data:{card_id:message.card_id}},this.uuid(`card:${task.id}`)));
      this.store.db.prepare('UPDATE task_messages SET message_id=? WHERE task_id=?').run(sent.message_id,task.id);
    }
    // Feishu streaming sessions close after 10 minutes. Switch to full-card updates before that limit.
    const closeStream=terminal(task.status)||Date.now()-task.created_at>9*60*1000;
    if(message.finalized){
      const {sequence}=this.store.nextSequence(task.id);
      await this.api(()=>this.feishu.raw('PUT',`/open-apis/cardkit/v1/cards/${message.card_id}`,{card:{type:'card_json',data:JSON.stringify(cardJson(task,content,false))},sequence,uuid:`u_${task.id}_${sequence}`}));
    }else{
      const {sequence}=this.store.nextSequence(task.id);
      try{await this.api(()=>this.feishu.raw('PUT',`/open-apis/cardkit/v1/cards/${message.card_id}/elements/task_content/content`,{content,sequence,uuid:`c_${task.id}_${sequence}`}));}
      catch(e){if(e instanceof FeishuError&&/stream|流式/i.test(e.message)){this.store.db.prepare('UPDATE task_messages SET finalized=1 WHERE task_id=?').run(task.id);}throw e;}
      if(closeStream){
        const {sequence}=this.store.nextSequence(task.id);
        await this.api(()=>this.feishu.raw('PATCH',`/open-apis/cardkit/v1/cards/${message.card_id}/settings`,{settings:JSON.stringify({config:{streaming_mode:false,summary:{content:`${labels[task.status]} · ${task.id.slice(0,8)}`}}}),sequence,uuid:`s_${task.id}_${sequence}`}));
        this.store.db.prepare('UPDATE task_messages SET finalized=1 WHERE task_id=?').run(task.id);
        // Refresh the title after the final stream update.
        const {sequence:next}=this.store.nextSequence(task.id);
        await this.api(()=>this.feishu.raw('PUT',`/open-apis/cardkit/v1/cards/${message.card_id}`,{card:{type:'card_json',data:JSON.stringify(cardJson(task,content,false))},sequence:next,uuid:`u_${task.id}_${next}`}));
      }
    }
    this.store.db.prepare('UPDATE task_messages SET last_revision=? WHERE task_id=?').run(task.last_revision,task.id);
  }
  private async collectArtifacts(task:Task){
    if(!task.workspace_path)return;
    const paths=artifactLinks(task.result);
    if(Buffer.byteLength(task.result)>12000){
      const file=path.join(task.workspace_path,`lark-result-${task.id}.md`);
      try{await fs.writeFile(file,task.result,{flag:'wx'});}catch(e:any){if(e.code!=='EEXIST')throw e;}paths.push(file);
    }
    for(const candidate of new Set(paths)){
      try{
        const file=await ownedArtifact(task.workspace_path,candidate),id=this.uuid(`${task.id}:${file.path}`);
        this.store.db.transaction(()=>{this.store.db.prepare('INSERT OR IGNORE INTO task_artifacts(id,task_id,path,name,size) VALUES(?,?,?,?,?)').run(id,task.id,file.path,file.name,file.size);
          const artifact=this.store.db.prepare('SELECT message_id FROM task_artifacts WHERE id=?').get(id) as any;
          if(!artifact.message_id)this.store.enqueue(`file:${id}`,task.id,'FILE',{artifactId:id});})();
      }catch(e){
        const reason=safeError(e),name=path.basename(candidate);this.store.event(task.id,'ARTIFACT_REJECTED',{name,reason});
        const id=`artifact-rejected:${task.id}:${this.uuid(candidate)}`;
        if(!this.store.db.prepare('SELECT 1 FROM delivery_outbox WHERE id=?').get(id))this.store.enqueue(id,task.id,'NOTICE',{
          chat:task.chat_id,message:task.source_message_id,text:`任务 ${task.id} 的附件未发送：${name}\n${reason}`,
        });
      }
    }
  }
  async stop(){this.stopping=true;clearInterval(this.timer);while(this.busy)await new Promise(r=>setTimeout(r,50));await this.gate.onIdle();}
}
