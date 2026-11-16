import type {Step} from '../types.js';

/**
 * What one turn of the agent reports, in order: the chat it runs in, each message of the agent once complete (`commentary` when tool calls came
 * with it, so it is on the way to the answer rather than the answer), each tool call as it starts and ends, and how the turn ended.
 */
export type TurnEvent =
  | {type:'session';id:string}
  | {type:'message';id:string;text:string;commentary:boolean}
  | {type:'tool';id:string;name:string;args:Record<string,unknown>;status:'running'|'done'|'failed';output?:string;exit?:number}
  | {type:'completed'}
  | {type:'failed';error:string};

// What is kept of a step is bounded: it is stored with the task and has to fit in a card.
const OUTPUT_KEEP=3000,ARGS_KEEP=800,RESULT_KEEP=1500;
const tail=(text:string,keep:number)=>text.length>keep?`…${text.slice(-keep)}`:text;
const head=(text:string,keep:number)=>text.length>keep?`${text.slice(0,keep)}…`:text;
const FILE_TOOLS:Record<string,string>={write:'write',edit:'edit'};

/** The visible shape of one event, or nothing for events that are not steps. */
function stepOf(event:TurnEvent):Step|undefined{
  switch(event.type){
    case 'message':return event.text?{id:event.id,type:'message',text:event.text,...(event.commentary?{commentary:true as const}:{})}:undefined;
    case 'tool':{
      if(event.name==='bash')return {id:event.id,type:'command',command:String(event.args.command??''),output:tail(event.output??'',OUTPUT_KEEP),status:event.status,
        ...(event.exit!==undefined?{exit:event.exit}:{})};
      if(FILE_TOOLS[event.name])return {id:event.id,type:'files',changes:[{path:String(event.args.path??''),kind:FILE_TOOLS[event.name]!}],status:event.status==='failed'?'failed':'done'};
      return {id:event.id,type:'tool',tool:event.name,args:head(JSON.stringify(event.args),ARGS_KEEP),result:head(event.output??'',RESULT_KEEP),status:event.status};
    }
    default:return undefined;
  }
}

/**
 * Folds the events of one turn into what a card shows: the steps in the order the agent took them. Each message arrives whole; while the turn
 * runs every message is progress, and once it completes the answer is picked out (see `answering`).
 */
export class Turn {
  sessionId='';
  state:'running'|'completed'|'failed'='running';
  private failure='';
  private byId=new Map<string,Step>();
  /** True when something a user can see has changed. */
  apply(event:TurnEvent):boolean{
    switch(event.type){
      case 'session':this.sessionId=event.id;return false;
      case 'completed':this.state='completed';return true;
      case 'failed':this.state='failed';this.failure=event.error;return true;
      default:{
        const step=stepOf(event);if(!step)return false;
        if(JSON.stringify(this.byId.get(step.id))===JSON.stringify(step))return false;
        this.byId.set(step.id,step);return true;
      }
    }
  }
  /** What the user sent while the turn ran, once the agent has taken it: shown where it came in. */
  steered(id:string,text:string,files:string[]){this.byId.set(id,{id,type:'user',text,files});}
  get steps():Step[]{return [...this.byId.values()];}
  /**
   * The messages that answer, once the turn has completed: the last one; or, when the user added something after the agent's last command or
   * tool, every message since then that is not commentary, since the agent may have finished the first request and then answered what came in.
   */
  private get answering():Step[]{
    if(this.state!=='completed')return [];
    const steps=this.steps,last=steps.findLastIndex(s=>s.type==='message');
    if(last<0)return [];
    const since=steps.slice(steps.findLastIndex((s,i)=>i<last&&s.type!=='message'&&s.type!=='user')+1,last+1);
    const answers=since.filter(s=>s.type==='message'&&!s.commentary);
    return since.some(s=>s.type==='user')&&answers.length?answers:[steps[last]!];
  }
  private text(steps:Step[]){return steps.flatMap(s=>s.type==='message'?[s.text]:[]).join('\n\n');}
  get progress(){const answering=new Set(this.answering);return this.text(this.steps.filter(s=>!answering.has(s)));}
  get answer(){return this.text(this.answering);}
  get error(){return this.state==='failed'?this.failure:'';}
}
