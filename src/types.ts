export const terminalStates = ['COMPLETED', 'FAILED', 'CANCELLED', 'INTERRUPTED'] as const;
export type TaskStatus = 'QUEUED'|'STARTING'|'RUNNING'|'WAITING_APPROVAL'|'COMPLETED'|'FAILED'|'CANCELLED'|'INTERRUPTED'|'UNKNOWN';
export const terminal = (status: string) => (terminalStates as readonly string[]).includes(status);
export interface Task {
  id: string; tenant_key: string; user_id: string; chat_id: string; source_message_id: string;
  instruction: string; desktop_host_id: string; thread_id: string|null; turn_id: string|null;
  workspace_path: string|null; status: TaskStatus; result: string; error: string;
  progress: string; last_revision: number; submission_key: string;
  created_at: number; started_at: number|null; finished_at: number|null;
  cancel_requested: number; resources: string; cleaned_at: number|null;
  session_id:string|null;
}
export interface UserSession {
  id:string;tenant_key:string;user_id:string;thread_id:string|null;workspace_path:string|null;created_at:number;
  display_name:string|null;title:string|null;applied_title:string|null;retired_at:number|null;
}
export interface TaskView {
  threadId: string; turnId: string; revision: number; status: string;
  commentary: string; answer: string; progress: {type: string; status: string}[];
  error: string; requests: PendingInteraction[];
  images: GeneratedImage[];
}
export interface GeneratedImage {id:string;savedPath:string|null;base64:string|null;}
export interface PendingInteraction {
  id: string; kind: 'command'|'file'|'permissions'|'input'|'elicitation';
  summary: string; payload: Record<string, any>;
}
export interface IncomingMessage {
  tenant: string; user: string; chat: string; message: string; text: string; createdAt?: number;
  attachments?: IncomingAttachment[]; error?: string;
}
export interface IncomingAttachment {kind:'image'|'file'; key:string; name:string;}
export interface TaskInput extends IncomingAttachment {ordinal:number; path:string|null; size:number|null;}
