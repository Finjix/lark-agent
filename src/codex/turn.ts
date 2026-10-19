import type {ThreadEvent} from '@openai/codex-sdk';

/**
 * Folds the events of one `codex exec` turn into what a card shows. Codex reports each message of the agent whole, once it is complete,
 * and does not say which one is the answer, so while the turn runs every message is progress and the last one becomes the answer when
 * the turn completes.
 */
export class Turn {
  threadId='';
  state:'running'|'completed'|'failed'='running';
  private failure='';
  private messages=new Map<string,string>();
  /** True when something a user can see has changed. */
  apply(event:ThreadEvent):boolean{
    switch(event.type){
      case 'thread.started':this.threadId=event.thread_id;return false;
      case 'item.updated':
      case 'item.completed':{
        const item=event.item;if(item.type!=='agent_message'||!item.text)return false;
        if(this.messages.get(item.id)===item.text)return false;
        this.messages.set(item.id,item.text);return true;
      }
      case 'turn.completed':this.state='completed';return true;
      case 'turn.failed':this.state='failed';this.failure=event.error.message;return true;
      // Reconnect notices and the like: Codex reports its own retries here, and the turn still ends with turn.completed or turn.failed.
      default:return false;
    }
  }
  get progress(){const all=[...this.messages.values()];return (this.state==='completed'?all.slice(0,-1):all).join('\n\n');}
  get answer(){return this.state==='completed'?[...this.messages.values()].at(-1)??'':'';}
  get error(){return this.state==='failed'?this.failure:'';}
}
