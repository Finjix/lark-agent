import type {ThreadEvent,ThreadItem} from '@openai/codex-sdk';
import type {Step} from '../types.js';

// What is kept of a step is bounded: it is stored with the task and has to fit in a card.
const OUTPUT_KEEP=3000,ARGS_KEEP=800,RESULT_KEEP=1500;
const tail=(text:string,keep:number)=>text.length>keep?`…${text.slice(-keep)}`:text;
const head=(text:string,keep:number)=>text.length>keep?`${text.slice(0,keep)}…`:text;
/** The shell Codex starts on Windows shows up in the command line it reports; what the user cares about is what it was asked to run. */
export function plainCommand(command:string){
  const wrapped=/^"?[^"]*?(?:powershell|pwsh)(?:\.exe)?"?\s+(?:-\w+\s+)*-Command\s+(.*)$/is.exec(command.trim());
  // `codex exec` quoted the command with double quotes, the app-server with single ones.
  return (wrapped?wrapped[1].replace(/^"([\s\S]*)"$|^'([\s\S]*)'$/,'$1$2'):command).trim();
}
function toolResult(item:Extract<ThreadItem,{type:'mcp_tool_call'}>){
  if(item.error)return item.error.message;
  return (item.result?.content??[]).map((block:any)=>typeof block?.text==='string'?block.text:`[${block?.type??'内容'}]`).join('\n');
}
/** The visible shape of one item of the turn, or nothing for items that are not shown (reasoning, Codex's own error notices). */
function stepOf(item:ThreadItem):Step|undefined{
  switch(item.type){
    // What Codex says on its way to the answer (the app-server's commentary phase) is marked: it is never part of the answer.
    case 'agent_message':return item.text?{id:item.id,type:'message',text:item.text,...((item as {phase?:string}).phase==='commentary'?{commentary:true as const}:{})}:undefined;
    case 'command_execution':return {id:item.id,type:'command',command:plainCommand(item.command),output:tail(item.aggregated_output??'',OUTPUT_KEEP),
      status:item.status==='in_progress'?'running':item.status==='failed'||(item.exit_code??0)!==0?'failed':'done',...(item.exit_code!==undefined?{exit:item.exit_code}:{})};
    case 'file_change':return {id:item.id,type:'files',changes:item.changes.map(c=>({path:c.path,kind:c.kind})),status:item.status==='failed'?'failed':'done'};
    case 'mcp_tool_call':return {id:item.id,type:'tool',server:item.server,tool:item.tool,args:head(JSON.stringify(item.arguments??{}),ARGS_KEEP),result:head(toolResult(item),RESULT_KEEP),
      status:item.status==='in_progress'?'running':item.status==='failed'?'failed':'done'};
    case 'web_search':return {id:item.id,type:'search',query:item.query};
    case 'todo_list':return {id:item.id,type:'todo',items:item.items.map(i=>({text:i.text,completed:i.completed}))};
    default:return undefined;
  }
}

/**
 * Folds the events of one `codex exec` turn into what a card shows: the steps in the order Codex took them (what it said, the commands it ran,
 * the files it changed). Codex reports each message of the agent whole, once it is complete, and does not say which one is the answer, so while
 * the turn runs every message is progress and the last one becomes the answer when the turn completes.
 */
export class Turn {
  threadId='';
  state:'running'|'completed'|'failed'='running';
  private failure='';
  private byId=new Map<string,Step>();
  /** True when something a user can see has changed. */
  apply(event:ThreadEvent):boolean{
    switch(event.type){
      case 'thread.started':this.threadId=event.thread_id;return false;
      case 'item.started':
      case 'item.updated':
      case 'item.completed':{
        const step=stepOf(event.item);if(!step)return false;
        if(JSON.stringify(this.byId.get(step.id))===JSON.stringify(step))return false;
        this.byId.set(step.id,step);return true;
      }
      case 'turn.completed':this.state='completed';return true;
      case 'turn.failed':this.state='failed';this.failure=event.error.message;return true;
      // Reconnect notices and the like: Codex reports its own retries here, and the turn still ends with turn.completed or turn.failed.
      default:return false;
    }
  }
  /** What the user sent while the turn ran, once Codex has taken it: shown where it came in. */
  steered(id:string,text:string,files:string[]){this.byId.set(id,{id,type:'user',text,files});}
  get steps():Step[]{return [...this.byId.values()];}
  /**
   * The messages that answer, once the turn has completed: the last one; or, when the user added something after Codex's last command or tool,
   * every message since then that is not commentary, since Codex may have finished the first request and then answered what came in. To-do
   * updates are no work.
   */
  private get answering():Step[]{
    if(this.state!=='completed')return [];
    const steps=this.steps.filter(s=>s.type!=='todo'),last=steps.findLastIndex(s=>s.type==='message');
    if(last<0)return [];
    const tail=steps.slice(steps.findLastIndex((s,i)=>i<last&&s.type!=='message'&&s.type!=='user')+1,last+1);
    const answers=tail.filter(s=>s.type==='message'&&!s.commentary);
    return tail.some(s=>s.type==='user')&&answers.length?answers:[steps[last]!];
  }
  private text(steps:Step[]){return steps.flatMap(s=>s.type==='message'?[s.text]:[]).join('\n\n');}
  get progress(){const answering=new Set(this.answering);return this.text(this.steps.filter(s=>!answering.has(s)));}
  get answer(){return this.text(this.answering);}
  get error(){return this.state==='failed'?this.failure:'';}
}
