import type {UserInput} from '@openai/codex-sdk';
import type {Turn} from '../codex/turn.js';

export type Steer=(input:UserInput[])=>Promise<void>;
/**
 * How the user's further messages reach one task's turn. The turn offers a way in once Codex takes input and withdraws it when it can no longer
 * take any; the task closes it for good when it is over. Hand-overs wait until there is a way in and go one after another, so messages reach
 * the turn in the order they were sent.
 */
export class Steering {
  /** The turn's steps, which show what the user added. */
  turn?:Turn;
  private steer?:Steer;private closed=false;private waiters:(()=>void)[]=[];private tail:Promise<unknown>=Promise.resolve();
  offer(steer:Steer|undefined){if(this.closed)return;this.steer=steer;if(steer)this.wake();}
  close(){this.closed=true;this.steer=undefined;this.wake();}
  private wake(){for(const waiter of [...this.waiters])waiter();}
  /**
   * The way into the turn once there is one; undefined once the task is over. `over` is asked every second too, for a task that ends without
   * ever having started (cancelled while queued, say).
   */
  async ready(over:()=>boolean):Promise<Steer|undefined>{
    while(!this.steer&&!this.closed&&!over())await new Promise<void>(resolve=>{
      const done=()=>{clearTimeout(timer);this.waiters=this.waiters.filter(w=>w!==done);resolve();};
      const timer=setTimeout(done,1000);timer.unref();this.waiters.push(done);
    });
    return this.closed?undefined:this.steer;
  }
  /** Runs `hand` after every hand-over before it. */
  serial<T>(hand:()=>Promise<T>):Promise<T>{const run=this.tail.then(hand);this.tail=run.catch(()=>{});return run;}
}
