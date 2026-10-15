import {terminal,type Task,type PendingInteraction} from '../types.js';
export const labels:Record<string,string>={QUEUED:'排队中',STARTING:'正在启动',RUNNING:'执行中',WAITING_APPROVAL:'等待你的确认',COMPLETED:'已完成',FAILED:'执行失败',CANCELLED:'已取消',INTERRUPTED:'已中断',UNKNOWN:'等待核对 Desktop'};
export function limitText(text:string,bytes=18000){
  if(Buffer.byteLength(text,'utf8')<=bytes)return text;
  let result='',size=0;for(const c of text){const n=Buffer.byteLength(c);if(size+n>bytes-80)break;result+=c;size+=n;}return result+'\n\n（内容较长，完整结果以附件为准）';
}
export const working=(task:Task)=>task.status==='STARTING'||task.status==='RUNNING';
export function elapsedSeconds(task:Task,now=Date.now()){return Math.max(0,Math.floor((now-(task.started_at??task.created_at))/1000));}
export function renderTask(task:Task,requests:PendingInteraction[]=[]){
  const parts:string[]=[];
  if(task.progress)parts.push(limitText(task.progress,7000));
  if(task.result)parts.push(limitText(task.result,12000));
  if(task.error)parts.push(limitText(task.error,1500));
  for(const r of requests){parts.push(`**需要${r.kind==='input'?'补充信息':'确认'}：${r.id}**\n${limitText(r.summary,2500)}\n`+
    (r.kind==='input'?`\`/answer ${task.id} ${r.id} 回答或JSON\``:`\`/approve ${task.id} ${r.id}\`\n\`/deny ${task.id} ${r.id}\``));}
  if(['FAILED','CANCELLED','INTERRUPTED'].includes(task.status))parts.push(labels[task.status]);
  if(!parts.length&&task.status==='COMPLETED')parts.push('本轮已完成。');
  return limitText(parts.join('\n\n'),24000);
}
export function messageSummary(content:string){return [...content.replace(/\s+/g,' ').trim()].slice(0,80).join('');}
export function cardJson(task:Task,content:string,streaming=!terminal(task.status),now=Date.now()){
  const title=`番薯搬砖中...... ${elapsedSeconds(task,now)}s`;
  return {schema:'2.0',config:{update_multi:true,streaming_mode:streaming,summary:{content:working(task)?title:messageSummary(content)}},
    ...(working(task)?{header:{title:{tag:'plain_text',content:title},template:'green'}}:{}),
    body:{elements:[{tag:'markdown',element_id:'task_content',content:content||' '}]}};
}
