import path from 'node:path';
import {config} from './config.js';
import type {Store} from './storage/database.js';
import type {Task,UserSession} from './types.js';
import {displayName,sessionStamp} from './sessions/naming.js';

/**
 * user/<user name>/data/        the only thing kept under user/: browser profile, Feishu CLI login, downloads
 *                               (a duplicate user name becomes name2, name3 ...)
 *
 * Inputs and outputs live in the folder Codex Desktop itself gives each chat (Documents\Codex\<date>\lark-<id>), where it can
 * write without approval; the bridge reads from and delivers out of there:
 *   <chat folder>/input/<yyyyMMdd_HHmmss>/    inbound attachments
 *   <chat folder>/output/<yyyyMMdd_HHmmss>/   deliverables, created by Codex only when a task produces something
 */
const illegal=/[<>:"/\\|?*\x00-\x1f]/g;
const clean=(value:string)=>displayName(value).replace(illegal,'_').replace(/[. ]+$/,'');

export function userDirectory(store:Store,session:Pick<UserSession,'tenant_key'|'user_id'|'display_name'>){
  const name=clean(session.display_name??'')||clean(session.user_id)||'user';
  return path.join(config.user,store.userDirName(session.tenant_key,session.user_id,name));
}
export const userData=(store:Store,session:Pick<UserSession,'tenant_key'|'user_id'|'display_name'>)=>path.join(userDirectory(store,session),'data');

/** A task's output folder is named by when its message arrived (output/20261016_162500); a clash within the same second gets _2. Fixed once stored on the task. */
export function taskWorkspace(store:Store,root:string,task:Pick<Task,'id'|'created_at'|'workspace_path'>){
  if(task.workspace_path)return task.workspace_path;
  const base=path.join(root,'output',sessionStamp(task.created_at));
  let directory=base;for(let n=2;store.taskWorkspaceTaken(directory,task.id);n++)directory=`${base}_${n}`;
  return directory;
}
export const sessionInputs=(sessionRoot:string)=>path.join(sessionRoot,'input');
