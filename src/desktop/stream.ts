import {EventEmitter} from 'node:events';
import type {TaskView,PendingInteraction} from '../types.js';

const forbidden=new Set(['__proto__','constructor','prototype']);
export function applyPatch(root:any,patch:any){
  if(!Array.isArray(patch.path)||!patch.path.length)throw Error('Unsupported root patch');
  let parent=root;
  for(const segment of patch.path.slice(0,-1)){
    if(forbidden.has(String(segment))||parent==null||!Object.hasOwn(parent,segment))throw Error('Invalid patch path'); parent=parent[segment];
  }
  const key=patch.path.at(-1);if(forbidden.has(String(key))||parent==null)throw Error('Invalid patch key');
  if(Array.isArray(parent)&&(!Number.isInteger(Number(key))||Number(key)<0||Number(key)>parent.length))throw Error('Invalid array index');
  if(patch.op==='remove'){if(!Object.hasOwn(parent,key))throw Error('Missing remove target');if(Array.isArray(parent))parent.splice(Number(key),1);else delete parent[key];}
  else if(patch.op==='add'||patch.op==='replace'){
    if(patch.op==='replace'&&!Object.hasOwn(parent,key))throw Error('Missing replace target');
    if(patch.op==='add'&&Array.isArray(parent))parent.splice(Number(key),0,structuredClone(patch.value));else parent[key]=structuredClone(patch.value);
  }else throw Error('Unsupported patch operation');
}
export function turnsOf(state:any):any[]{return state?.turnHistory?.kind==='canonical'?Object.values(state.turnHistory.history.entitiesByKey??{}):state?.turns??[];}
export function interactionsOf(state:any,turnId:string):PendingInteraction[]{
  const requests=Array.isArray(state.requests)?state.requests:Object.values(state.requests??{});
  return requests.flatMap((r:any)=>{
    const p=r.params??r.request?.params??{},method=r.method??r.request?.method??'';
    if(p.turnId&&p.turnId!==turnId)return [];
    const kind:PendingInteraction['kind']|undefined=({
      'item/commandExecution/requestApproval':'command','item/fileChange/requestApproval':'file',
      'item/permissions/requestApproval':'permissions','item/tool/requestUserInput':'input',
      'mcpServer/elicitation/request':'elicitation',
    } as Record<string,PendingInteraction['kind']>)[method];
    if(!kind)return [];
    return [{id:String(r.id??r.request?.id),kind,summary:String(p.reason??p.message??p.command??(p.questions?JSON.stringify(p.questions):kind)).slice(0,3000),payload:{...p,_requestId:r.id??r.request?.id}}];
  });
}
export class StreamState extends EventEmitter {
  states=new Map<string,{revision:number;state:any}>();
  receive(message:any){
    if(message.type!=='broadcast'||message.method!=='thread-stream-state-changed'||message.params?.hostId!=='local')return;
    const {conversationId:id,change}=message.params;if(!id||!change||!Number.isInteger(change.revision))return;
    const old=this.states.get(id);
    if(old&&change.revision<old.revision)return;
    if(change.type==='snapshot')this.states.set(id,{revision:change.revision,state:structuredClone(change.conversationState)});
    else if(change.type==='patches'){
      if(old&&change.revision<=old.revision)return;
      if(!old||change.baseRevision!==old.revision){this.emit('resync',id);return;}
      try{const state=structuredClone(old.state);for(const p of change.patches??[])applyPatch(state,p);this.states.set(id,{revision:change.revision,state});}
      catch{this.emit('resync',id);return;}
    }else return;
    this.emit('state',id);
  }
  view(threadId:string,turnId?:string|null,marker?:string,clientUserMessageId?:string):TaskView|undefined {
    const cached=this.states.get(threadId);if(!cached)return;
    const turns=turnsOf(cached.state);
    const matching=clientUserMessageId?turns.filter(t=>t.params?.clientUserMessageId===clientUserMessageId):[];
    const turn=turnId?turns.find(t=>t.turnId===turnId):clientUserMessageId?(matching.length===1?matching[0]:undefined):turns.find(t=>
      JSON.stringify(t.params?.input??[]).includes(marker??'__never__')||
      (typeof t.params?.toolOutput?.output==='string'&&t.params.toolOutput.output.includes(marker??'__never__'))||
      (t.items??[]).some((i:any)=>i.type==='userMessage'&&JSON.stringify(i.content??i.text??'').includes(marker??'__never__')));
    if(!turn)return;
    const items=turn.items??[],messages=items.filter((i:any)=>i.type==='agentMessage'&&typeof i.text==='string');
    return {threadId,turnId:turn.turnId,revision:cached.revision,status:turn.status,
      commentary:messages.filter((i:any)=>i.phase==='commentary').map((i:any)=>i.text).join('\n\n'),
      answer:messages.filter((i:any)=>i.phase==='final_answer').map((i:any)=>i.text).join('\n\n'),
      images:items.filter((i:any)=>i.type==='imageGeneration'&&i.status==='completed'&&!i.failure).map((i:any)=>({
        id:String(i.id),savedPath:typeof i.savedPath==='string'&&i.savedPath?i.savedPath:null,
        base64:!i.savedPath&&typeof i.result==='string'?i.result:null,
      })),
      progress:items.filter((i:any)=>['commandExecution','fileChange','mcpToolCall','dynamicToolCall'].includes(i.type)).map((i:any)=>({type:i.type,status:i.status??'inProgress'})),
      error:typeof turn.error==='string'?turn.error:turn.error?.message??'',requests:interactionsOf(cached.state,turn.turnId)};
  }
}
