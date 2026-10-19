import fs from 'node:fs/promises';
import path from 'node:path';
import {config} from './config.js';
import {Store} from './storage/database.js';
import {log,safeError} from './log.js';
import {removeInputs} from './feishu/inputs.js';
import {workspaceIn} from './layout.js';

const DAY=86400000;
/**
 * Removes one task's own files once the retention period is over: what it delivered and the attachments it received. The workspace, the user's chat
 * and other tasks' files stay. A task whose folder is not the workspace of its session is skipped, and says so in the log: nothing is removed on a guess.
 */
export async function cleanup(store:Store){
  for(const task of store.expired(Date.now()-config.RETENTION_DAYS*DAY)){
    if(store.db.prepare("SELECT 1 FROM delivery_outbox WHERE task_id=? AND status!='DELIVERED' LIMIT 1").get(task.id))continue;
    if((!task.workspace_path||!task.thread_id)&&!store.inputs(task.id).length)continue;
    try{
      const session=task.session_id?store.session(task.session_id):undefined;
      if(task.workspace_path&&task.thread_id){
        const target=path.resolve(task.workspace_path);let actual:string|undefined;
        // A directory that is already gone needs no removal, but the task must still be marked cleaned or it is retried (and logged) forever.
        try{actual=await fs.realpath(target);}catch(e:any){if(e.code!=='ENOENT')throw e;}
        if(actual){
          const sessionRoot=session?.workspace_path?await fs.realpath(session.workspace_path):'';
          if(!sessionRoot||actual!==target||actual!==sessionRoot)throw Error('目录不满足会话任务所有权检查，跳过清理');
          // The workspace is shared by all of the user's tasks and stays; only the files this task delivered go.
          for(const {path:file} of store.db.prepare('SELECT path FROM task_artifacts WHERE task_id=?').all(task.id) as {path:string}[]){
            const rel=path.relative(actual,file);
            if(!rel||rel.startsWith('..')||path.isAbsolute(rel))continue;
            await fs.rm(file,{force:true});
          }
        }
      }
      if(session?.workspace_path)await removeInputs(store.inputs(task.id).flatMap(i=>i.path?[i.path]:[]),session.workspace_path);
      store.update(task.id,{cleaned_at:Date.now()});store.event(task.id,'CLEANED',{});
    }catch(e){log.warn({taskId:task.id,error:safeError(e)},'保留策略清理已跳过');}
  }
}

/** What a sweep took away. */
export interface Removed {files:number;bytes:number}
const nothing=():Removed=>({files:0,bytes:0});
const addTo=(total:Removed,more:Removed)=>{total.files+=more.files;total.bytes+=more.bytes;};
/** A real folder. A link (symlink, junction) is not one: lstat does not follow it, so nothing outside the swept folder is reached through it. */
const plainDirectory=async(directory:string)=>(await fs.lstat(directory).catch(()=>undefined))?.isDirectory()===true;
/**
 * Deletes what under `root` nobody has touched since `cutoff`. A file's age is its most recent creation, change or move, so what is still
 * in use stays however long ago it was made; `only` limits the deletion to matching file names. A link is deleted as a link and never entered.
 * A folder left empty and created before the cutoff goes as well, `root` itself stays. What cannot be deleted now (open in a program,
 * no permission) is left for the next pass.
 */
async function sweep(root:string,cutoff:number,signal?:AbortSignal,only?:RegExp):Promise<Removed>{
  const removed=nothing();
  if(!await plainDirectory(root))return removed;
  const walk=async(directory:string)=>{
    for(const entry of await fs.readdir(directory,{withFileTypes:true})){
      if(signal?.aborted)return;
      const target=path.join(directory,entry.name);
      try{
        if(entry.isDirectory()){
          await walk(target);
          // rmdir refuses a folder that still holds something, which is the answer.
          if((await fs.lstat(target)).birthtimeMs<cutoff)await fs.rmdir(target).catch(()=>{});
        }else if(!only||only.test(entry.name)){
          const stat=await fs.lstat(target);
          if(Math.max(stat.birthtimeMs,stat.mtimeMs,stat.ctimeMs)>=cutoff)continue;
          await fs.rm(target,{force:true});removed.files++;removed.bytes+=stat.isFile()?stat.size:0;
        }
      }catch(e){log.debug({path:target,error:safeError(e)},'清理时跳过一项');}
    }
  };
  try{await walk(root);}catch(e){log.debug({path:root,error:safeError(e)},'清理时无法读取目录');}
  return removed;
}

/**
 * What of Codex's home may go once it is old, relative to it. Everything else there stays: its databases (they also hold the chats), the login,
 * the configuration, the installation id, its skills and the temporary files of running processes; of the Windows sandbox's folder, which
 * keeps its setup state next to its logs, only the logs.
 */
const CODEX_EXPENDABLE:{folder:string;only?:RegExp}[]=[
  {folder:'sessions'},{folder:'archived_sessions'},{folder:'generated_images'},{folder:'shell_snapshots'},{folder:'log'},{folder:'.tmp'},
  {folder:'.sandbox',only:/\.log$/i},
];
export interface Purged {logs:Removed;workspaces:Removed;codex:Removed}
type Roots=Pick<typeof config,'data'|'user'|'codexHome'>;
const usable=(name:string)=>!!name&&name!=='.'&&name!=='..'&&path.basename(name)===name;
/**
 * Deletes what has not been touched for RETENTION_DAYS: the service's own logs, every file in the users' workspaces (what Codex made, what
 * the users sent), and the expendable files of Codex's home. Backups, the database and everything else stay. A user with a task queued
 * or running keeps their workspace untouched, and Codex's files are left alone while any task runs.
 */
export async function purge(store:Store,options:{now?:number;roots?:Roots;signal?:AbortSignal}={}):Promise<Purged>{
  const {now=Date.now(),roots=config,signal}=options,cutoff=now-config.RETENTION_DAYS*DAY;
  const purged:Purged={logs:nothing(),workspaces:nothing(),codex:nothing()};
  addTo(purged.logs,await sweep(path.join(roots.data,'logs'),cutoff,signal,/\.jsonl$/));
  for(const {tenant_key,user_id,name} of store.userDirs()){
    if(signal?.aborted)return purged;
    if(!usable(name)||store.busy(tenant_key,user_id))continue;
    const directory=path.join(roots.user,name);
    if(await plainDirectory(directory))addTo(purged.workspaces,await sweep(workspaceIn(directory),cutoff,signal));
  }
  if(!store.active().length)for(const {folder,only} of CODEX_EXPENDABLE)addTo(purged.codex,await sweep(path.join(roots.codexHome,folder),cutoff,signal,only));
  return purged;
}

let running:Promise<void>|undefined;
/** One round of housekeeping, run at start and then every 24 hours. It never rejects, and a round that is still running is not started a second time. */
export function housekeeping(store:Store,signal?:AbortSignal):Promise<void>{
  return running??=(async()=>{
    try{await cleanup(store);}catch(e){log.warn({error:safeError(e)},'保留策略清理失败');}
    try{
      const purged=await purge(store,{signal});
      if(purged.logs.files+purged.workspaces.files+purged.codex.files)log.info(purged,'已清理超过保留期的数据');
    }catch(e){log.warn({error:safeError(e)},'过期数据清理失败');}
  })().finally(()=>{running=undefined;});
}
