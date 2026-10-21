import {terminal,BOT_DOWN,type Task,type Step} from '../types.js';
export const labels:Record<string,string>={QUEUED:'排队中',STARTING:'正在启动',RUNNING:'执行中',COMPLETED:'已完成',FAILED:'执行失败',CANCELLED:'已取消',INTERRUPTED:'已中断'};
export function limitText(text:string,bytes=18000){
  if(Buffer.byteLength(text,'utf8')<=bytes)return text;
  let result='',size=0;for(const c of text){const n=Buffer.byteLength(c);if(size+n>bytes-80)break;result+=c;size+=n;}return result+'\n\n（内容较长，完整结果以附件为准）';
}
export const working=(task:Task)=>task.status==='STARTING'||task.status==='RUNNING';
export function elapsedSeconds(task:Task,now=Date.now()){return Math.max(0,Math.floor((now-(task.started_at??task.created_at))/1000));}
/** Feishu rejects the whole card if a markdown image is anything but one of its own image keys (Codex writes data: URIs, local paths and web addresses); generated images are sent as separate messages. */
export const withoutImages=(text:string)=>text.replace(/!\[([^\]]*)\]\((<[^>\n]*>|[^)\n]*)\)/g,(_all,alt:string,url:string)=>{
  // A web address stays as a plain link (Feishu cannot show it as an image but the user can open it); data: URIs and local paths are dropped.
  const target=url.replace(/^<|>$/g,'').trim();return /^https?:\/\//i.test(target)?`[${alt||target}](${target})`:'';
}).replace(/\n{3,}/g,'\n\n').trim();
/** What follows the text of a task: why it failed, and how it ended. */
function tailParts(task:Task){
  const parts:string[]=[];
  if(task.error)parts.push(limitText(task.error,1500));
  // The bot-down sentence stands alone: no status label after it.
  if(['FAILED','CANCELLED','INTERRUPTED'].includes(task.status)&&task.error!==BOT_DOWN)parts.push(labels[task.status]);
  return parts;
}
/** The whole reply as text: used for the preview in the chat list and to tell an empty card from one with something to show. */
export function renderTask(task:Task){
  const parts:string[]=[];
  if(withoutImages(task.progress))parts.push(limitText(withoutImages(task.progress),7000));
  if(withoutImages(task.result))parts.push(limitText(withoutImages(task.result),12000));
  parts.push(...tailParts(task));
  if(!parts.length&&task.status==='COMPLETED')parts.push('本轮已完成。');
  return limitText(parts.join('\n\n'),24000);
}
export function messageSummary(content:string){return [...content.replace(/\s+/g,' ').trim()].slice(0,80).join('');}

// ---- the card body: Codex's steps in order, much as Codex itself lists them
const LONG_MESSAGE=600,CARD_BYTES=26000;
const oneLine=(text:string,max:number)=>{const line=text.replace(/\s+/g,' ').trim();return line.length>max?`${line.slice(0,max)}…`:line;};
const icon=(status:string)=>status==='running'?'⏳':status==='failed'?'❌':'✅';
// Backticks inside a code block would end it early.
const code=(text:string)=>'```\n'+text.replaceAll('```','ʼʼʼ')+'\n```';
const markdown=(content:string)=>({tag:'markdown',content});
/** A folded block that opens on a tap: tool calls and long text sit in these, so the answer is what the eye lands on. */
const panel=(title:string,body:string)=>({tag:'collapsible_panel',expanded:false,header:{title:{tag:'plain_text',content:title}},elements:[markdown(body||' ')]});
function parseSteps(task:Task):Step[]{try{const steps=JSON.parse(task.steps??'[]');return Array.isArray(steps)?steps:[];}catch{return [];}}
function stepElement(step:Step,last:boolean){
  switch(step.type){
    case 'message':{
      const text=limitText(withoutImages(step.text),12000);if(!text)return;
      return last||text.length<=LONG_MESSAGE?markdown(text):panel(`💬 ${oneLine(text,40)}`,text);
    }
    // Commands, file changes and tool calls are not shown on the card: the user wants the answer, not the plumbing (what is made for them
    // arrives as files). They stay in the stored steps.
    case 'command':return;
    case 'files':return;
    case 'tool':return;
    case 'search':return markdown(`🔍 搜索：${oneLine(step.query,120)}`);
    case 'todo':return markdown(step.items.map(i=>`${i.completed?'☑':'☐'} ${i.text}`).join('\n'));
  }
}
/** A card holds about 30 KB. When a long task has more than that, the oldest panels give up their contents first, then the oldest steps are left out. */
function fit(elements:any[]){
  const size=()=>Buffer.byteLength(JSON.stringify(elements));
  for(const element of elements){if(size()<=CARD_BYTES)return elements;if(element.tag==='collapsible_panel')element.elements=[markdown('（内容已省略）')];}
  let omitted=0;
  while(size()>CARD_BYTES&&elements.length>1){elements.shift();omitted++;}
  return omitted?[markdown(`（前面 ${omitted} 步已省略）`),...elements]:elements;
}
export function cardElements(task:Task,content:string){
  const steps=parseSteps(task),lastMessage=steps.map(s=>s.type).lastIndexOf('message');
  const elements=steps.flatMap((step,i)=>{const element=stepElement(step,i===lastMessage);return element?[element]:[];});
  if(!elements.length)return [{tag:'markdown',element_id:'task_content',content:content||' '}];
  const tail=tailParts(task);if(tail.length)elements.push(markdown(tail.join('\n\n')));
  return fit(elements);
}
export function cardJson(task:Task,content:string,streaming=!terminal(task.status),now=Date.now()){
  const title=`番薯搬砖中...... ${elapsedSeconds(task,now)}s`;
  return {schema:'2.0',config:{update_multi:true,streaming_mode:streaming,summary:{content:working(task)?title:messageSummary(content)}},
    ...(working(task)?{header:{title:{tag:'plain_text',content:title},template:'green'}}:{}),
    body:{elements:cardElements(task,content)}};
}
