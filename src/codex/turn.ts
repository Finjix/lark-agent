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
    case 'agent_message':return item.text?{id:item.id,type:'message',text:item.text}:undefined;
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
  private get messages(){return this.steps.flatMap(s=>s.type==='message'?[s.text]:[]);}
  get progress(){const all=this.messages;return (this.state==='completed'?all.slice(0,-1):all).join('\n\n');}
  get answer(){return this.state==='completed'?this.messages.at(-1)??'':'';}
  get error(){return this.state==='failed'?this.failure:'';}
}
