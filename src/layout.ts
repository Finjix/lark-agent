import path from 'node:path';
import {config} from './config.js';
import type {Store} from './storage/database.js';
import type {UserSession} from './types.js';
import {displayName,sessionStamp} from './sessions/naming.js';

/**
 * user/
 *   <user name>/             one per Feishu user (a duplicate name becomes name2, name3 ...)
 *     data/                  user-level data that outlives sessions: browser profile, Feishu CLI login, downloads
 *     session/
 *       <yyyyMMdd_HHmmss>/   one per Desktop chat
 *         input/<taskId>/    inbound attachments
 *         output/<taskId>/   deliverables
 */
const illegal=/[<>:"/\\|?*\x00-\x1f]/g;
const clean=(value:string)=>displayName(value).replace(illegal,'_').replace(/[. ]+$/,'');

export function userDirectory(store:Store,session:Pick<UserSession,'tenant_key'|'user_id'|'display_name'>){
  const name=clean(session.display_name??'')||clean(session.user_id)||'user';
  return path.join(config.user,store.userDirName(session.tenant_key,session.user_id,name));
}
export const userData=(store:Store,session:Pick<UserSession,'tenant_key'|'user_id'|'display_name'>)=>path.join(userDirectory(store,session),'data');

/** The session folder, fixed when the session is bound so later renames do not move files. */
export function sessionDirectory(store:Store,session:UserSession){
  if(session.workspace_path)return session.workspace_path;
  const base=path.join(userDirectory(store,session),'session',sessionStamp(session.created_at));
  let directory=base;for(let n=2;store.workspaceTaken(directory,session.id);n++)directory=`${base}_${n}`;
  return directory;
}
export function taskWorkspace(root:string,taskId:string){
  if(!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(taskId))throw Error('无效的任务标识');
  return path.join(root,'output',taskId);
}
/** Sessions created before the user/<name>/session layout kept deliverables under tasks/ and attachments under inputs/. */
export const legacyTaskWorkspace=(root:string,taskId:string)=>path.join(root,'tasks',taskId);
export const sessionInputs=(sessionRoot:string)=>path.join(sessionRoot,'input');
