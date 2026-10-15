import {terminal,type Task,type PendingInteraction} from '../types.js';
export const labels:Record<string,string>={QUEUED:'排队中',STARTING:'正在启动',RUNNING:'执行中',WAITING_APPROVAL:'等待你的确认',COMPLETED:'已完成',FAILED:'执行失败',CANCELLED:'已取消',INTERRUPTED:'已中断',UNKNOWN:'等待核对 Desktop'};
export function limitText(text:string,bytes=18000){
  if(Buffer.byteLength(text,'utf8')<=bytes)return text;
  let result='',size=0;for(const c of text){const n=Buffer.byteLength(c);if(size+n>bytes-80)break;result+=c;size+=n;}return result+'\n\n（内容较长，完整结果以附件为准）';
}
export function renderTask(task:Task,requests:PendingInteraction[]=[]){
  const parts=[`**任务 ${task.id}**\n**状态：${labels[task.status]}**`];
  if(task.progress)parts.push(`**进度**\n${limitText(task.progress,7000)}`);
  if(task.result)parts.push(`**回复**\n${limitText(task.result,12000)}`);
  if(task.error)parts.push(`**说明**\n${limitText(task.error,1500)}`);
  for(const r of requests){parts.push(`**需要${r.kind==='input'?'补充信息':'确认'}：${r.id}**\n${limitText(r.summary,2500)}\n`+
    (r.kind==='input'?`\`/answer ${task.id} ${r.id} 回答或JSON\``:`\`/approve ${task.id} ${r.id}\`\n\`/deny ${task.id} ${r.id}\``));}
  if(task.finished_at&&task.started_at)parts.push(`耗时 ${Math.round((task.finished_at-task.started_at)/1000)} 秒`);
  parts.push(`查询：\`/status ${task.id}\``);
  return limitText(parts.join('\n\n'),24000);
}
export function cardJson(task:Task,content:string,streaming=!terminal(task.status)){
  return {schema:'2.0',config:{streaming_mode:streaming,summary:{content:`${labels[task.status]} · ${task.id.slice(0,8)}`}},
    header:{title:{tag:'plain_text',content:`任务 ${task.id.slice(0,8)} · ${labels[task.status]}`},template:task.status==='FAILED'?'red':task.status==='COMPLETED'?'green':'blue'},
    body:{elements:[{tag:'markdown',element_id:'task_content',content}]}};
}
