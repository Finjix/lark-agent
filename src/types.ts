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
}
export interface TaskView {
  threadId: string; turnId: string; revision: number; status: string;
  commentary: string; answer: string; progress: {type: string; status: string}[];
  error: string; requests: PendingInteraction[];
}
export interface PendingInteraction {
  id: string; kind: 'command'|'file'|'permissions'|'input'|'elicitation';
  summary: string; payload: Record<string, any>;
}
export interface IncomingMessage {
  tenant: string; user: string; chat: string; message: string; text: string; isGroup: boolean;
}
export interface DesktopAdapter {
  ready: boolean;
  probe(): Promise<Record<string, unknown>>;
  startTask(task: Task): Promise<{threadId: string; workspace: string}>;
  follow(threadId: string): Promise<void>;
  readTask(threadId: string): Promise<void>;
  interrupt(threadId: string, turnId: string): Promise<void>;
  respond(threadId: string, turnId: string, request: PendingInteraction, response: string): Promise<void>;
  archive(threadId: string): Promise<void>;
  unfollow(threadId: string): void;
  close(): void;
}
