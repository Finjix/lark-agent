import fs from 'node:fs/promises';
import path from 'node:path';
import {config} from './config.js';
import {Store} from './storage/database.js';
import {NativeDesktop} from './desktop/adapter.js';
import {log,safeError} from './log.js';
import {removeInputs} from './feishu/inputs.js';
import {taskWorkspace,legacyTaskWorkspace} from './layout.js';
/** Removes one task's own files once the retention period is over; the chat, its folders and other tasks' files stay. */
export async function cleanup(store:Store,desktop:NativeDesktop){
  for(const task of store.expired(Date.now()-config.TASK_RETENTION_DAYS*86400000)){
    if(store.db.prepare("SELECT 1 FROM delivery_outbox WHERE task_id=? AND status!='DELIVERED' LIMIT 1").get(task.id))continue;
    if((!task.workspace_path||!task.thread_id)&&!store.inputs(task.id).length)continue;
    try{
      const session=task.session_id?store.session(task.session_id):undefined;
      if(task.workspace_path&&task.thread_id){
        const target=path.resolve(task.workspace_path);let actual:string|undefined;
        // A directory that is already gone needs no removal, but the task must still be marked cleaned or it is retried (and logged) forever.
        try{actual=await fs.realpath(target);}catch(e:any){if(e.code!=='ENOENT')throw e;}
        if(actual&&session){
          const sessionRoot=session.workspace_path?await fs.realpath(session.workspace_path):'';
          if(!session.workspace_path||actual!==target)throw Error('目录不满足会话任务所有权检查，跳过清理');
          if(actual===taskWorkspace(sessionRoot)||actual===legacyTaskWorkspace(sessionRoot)){
            // The shared outputs folder (or its earlier name, output) stays; only the files this task delivered go.
            for(const {path:file} of store.db.prepare('SELECT path FROM task_artifacts WHERE task_id=?').all(task.id) as {path:string}[]){
              const rel=path.relative(actual,file);
              if(!rel||rel.startsWith('..')||path.isAbsolute(rel))continue;
              await fs.rm(file,{force:true});
            }
          }else if(path.dirname(path.dirname(actual))===sessionRoot&&['output','tasks'].includes(path.basename(path.dirname(actual)))){
            // Earlier layouts gave every task its own folder.
            await fs.rm(actual,{recursive:true,force:false});
          }else throw Error('目录不满足会话任务所有权检查，跳过清理');
        }else if(actual){
          if(actual!==target||path.basename(target)!=='outputs'||path.basename(path.dirname(target))!==`lark-${task.id}`)throw Error('目录不满足机器人所有权检查，跳过清理');
          await desktop.archive(task.thread_id);await fs.rm(actual,{recursive:true,force:false});
        }
      }
      if(session?.workspace_path)await removeInputs(store.inputs(task.id).flatMap(i=>i.path?[i.path]:[]),session.workspace_path);
      store.db.prepare('UPDATE task_generated_images SET base64=NULL WHERE task_id=?').run(task.id);
      store.update(task.id,{cleaned_at:Date.now()});store.event(task.id,'CLEANED',{});
    }catch(e){log.warn({taskId:task.id,error:safeError(e)},'保留策略清理已跳过');}
  }
}
