export const terminalStates = ['COMPLETED', 'FAILED', 'CANCELLED', 'INTERRUPTED'] as const;
export type TaskStatus = 'QUEUED'|'STARTING'|'RUNNING'|'COMPLETED'|'FAILED'|'CANCELLED'|'INTERRUPTED';
export const terminal = (status: string) => (terminalStates as readonly string[]).includes(status);
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
/** One thing that happened in a turn, in the order it did: what Codex said, a command it ran, files it changed, a tool, a search, its to-do list. Stored with the task as JSON. */
export type Step =
  | {id:string;type:'message';text:string}
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
export interface TaskInput extends IncomingAttachment {ordinal:number; path:string|null; size:number|null;}
