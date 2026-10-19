import path from 'node:path';
import {config} from './config.js';
import type {Store} from './storage/database.js';
import type {UserSession} from './types.js';
import {displayName} from './sessions/naming.js';

/**
 * user/<user name>/workspace/   Codex's working directory for this user (a duplicate user name becomes name2, name3 ...):
 *                               attachments arrive in input/, and the files Codex creates or changes while a task runs are delivered
 */
const illegal=/[<>:"/\\|?*\x00-\x1f]/g;
const clean=(value:string)=>displayName(value).replace(illegal,'_').replace(/[. ]+$/,'');

export function userDirectory(store:Store,session:Pick<UserSession,'tenant_key'|'user_id'|'display_name'>){
  const name=clean(session.display_name??'')||clean(session.user_id)||'user';
  return path.join(config.user,store.userDirName(session.tenant_key,session.user_id,name));
}
export const workspaceIn=(userDirectory:string)=>path.join(userDirectory,'workspace');
export const userWorkspace=(store:Store,session:Pick<UserSession,'tenant_key'|'user_id'|'display_name'>)=>workspaceIn(userDirectory(store,session));

/** Folder names of the earlier layouts, where deliveries came from <chat folder>/outputs (before that output); tasks recorded with them are still cleaned up. */
export const taskWorkspace=(chatFolder:string)=>path.join(chatFolder,'outputs');
export const legacyTaskWorkspace=(chatFolder:string)=>path.join(chatFolder,'output');
export const sessionInputs=(sessionRoot:string)=>path.join(sessionRoot,'input');
