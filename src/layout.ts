import path from 'node:path';
import {config} from './config.js';
import type {Store} from './storage/database.js';
import type {UserSession} from './types.js';
import {displayName} from './sessions/naming.js';

/**
 * user/<user name>/data/        the only thing kept under user/: browser profile, Feishu CLI login, downloads
 *                               (a duplicate user name becomes name2, name3 ...)
 *
 * Files are exchanged in the folder Codex Desktop gives each chat (Documents\Codex\<date>\lark-<id>), where it can write
 * without approval: attachments arrive in <chat folder>/input, and what Codex puts in <chat folder>/outputs (the folder Desktop itself creates in every chat) is delivered.
 */
const illegal=/[<>:"/\\|?*\x00-\x1f]/g;
const clean=(value:string)=>displayName(value).replace(illegal,'_').replace(/[. ]+$/,'');

export function userDirectory(store:Store,session:Pick<UserSession,'tenant_key'|'user_id'|'display_name'>){
  const name=clean(session.display_name??'')||clean(session.user_id)||'user';
  return path.join(config.user,store.userDirName(session.tenant_key,session.user_id,name));
}
export const userData=(store:Store,session:Pick<UserSession,'tenant_key'|'user_id'|'display_name'>)=>path.join(userDirectory(store,session),'data');

/** Everything Codex wants to send to the user goes in this one folder; no naming scheme is imposed on the files. */
export const OUTPUT_DIR='outputs';
export const taskWorkspace=(chatFolder:string)=>path.join(chatFolder,OUTPUT_DIR);
/** Folder name used before the rename to outputs; tasks recorded with it are still cleaned up. */
export const legacyTaskWorkspace=(chatFolder:string)=>path.join(chatFolder,'output');
export const sessionInputs=(sessionRoot:string)=>path.join(sessionRoot,'input');
