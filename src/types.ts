/**
 * A message sent while the user's previous one is still unfinished is STEERING while it is handed to that task's turn (steering, see
 * TaskService.join) and STEERED once the turn has taken it: it is then part of that task, which answers it, and has no card of its own.
 */
export const terminalStates = ['COMPLETED', 'FAILED', 'CANCELLED', 'INTERRUPTED', 'STEERED'] as const;
export type TaskStatus = 'QUEUED'|'STEERING'|'STARTING'|'RUNNING'|'COMPLETED'|'FAILED'|'CANCELLED'|'INTERRUPTED'|'STEERED';
export const terminal = (status: string) => (terminalStates as readonly string[]).includes(status);
/** Not started: waiting for a turn of its own, or for the running task's turn to take it. Neither holds the user's turn or a resource. */
export const waitingStates = ['QUEUED', 'STEERING'] as const;
export const waiting = (status: string) => (waitingStates as readonly string[]).includes(status);
const sqlList = (states: readonly string[]) => `(${states.map(s=>`'${s}'`).join(',')})`;
/** The terminal and the waiting states as SQL lists. */
export const TERMINAL_SQL = sqlList(terminalStates), WAITING_SQL = sqlList(waitingStates);
/** The one thing users are told whenever the bot cannot do its job (Codex unavailable or failing, attachments failing); the cause goes to the log. */
export const BOT_DOWN = '机器人罢工了，请联系 @钟丰骏-实习生';
export interface Task {
  id: string; tenant_key: string; user_id: string; chat_id: string; source_message_id: string;
  instruction: string; thread_id: string|null;
  workspace_path: string|null; status: TaskStatus; result: string; error: string;
  progress: string; steps: string;
  created_at: number; started_at: number|null; finished_at: number|null;
  cancel_requested: number; resources: string; cleaned_at: number|null;
  session_id:string|null;
}
/**
 * One thing that happened in a turn, in the order it did: what Codex said, a command it ran, files it changed, a tool, a search, its to-do
 * list, and what the user added while it ran (`user`, with the names of the files that came with it). Stored with the task as JSON.
 */
export type Step =
  | {id:string;type:'message';text:string;commentary?:true}
  | {id:string;type:'user';text:string;files:string[]}
  | {id:string;type:'command';command:string;output:string;status:'running'|'done'|'failed';exit?:number}
  | {id:string;type:'files';changes:{path:string;kind:string}[];status:'done'|'failed'}
  | {id:string;type:'tool';server:string;tool:string;args:string;result:string;status:'running'|'done'|'failed'}
  | {id:string;type:'search';query:string}
  | {id:string;type:'todo';items:{text:string;completed:boolean}[]};
export interface UserSession {
  id:string;tenant_key:string;user_id:string;thread_id:string|null;workspace_path:string|null;created_at:number;
  display_name:string|null;retired_at:number|null;
}
export interface GeneratedImage {id:string;savedPath:string;}
export interface IncomingMessage {
  tenant: string; user: string; chat: string; message: string; text: string; createdAt?: number;
  attachments?: IncomingAttachment[]; error?: string;
}
export interface IncomingAttachment {kind:'image'|'file'; key:string; name:string;}
/** An attachment the user has to send differently (too large, say): its message is shown to them instead of BOT_DOWN. */
export class InputRejected extends Error {}
export interface TaskInput extends IncomingAttachment {ordinal:number; path:string|null; size:number|null;}
