import fs from 'node:fs/promises';
import path from 'node:path';
import {config} from './config.js';
import {Store} from './storage/database.js';
import {NativeDesktop} from './desktop/adapter.js';
import {terminal} from './types.js';
import {log,safeError} from './log.js';
import {removeInputs} from './feishu/inputs.js';
import {taskWorkspace} from './desktop/adapter.js';
export async function cleanup(store:Store,desktop:NativeDesktop){
  for(const task of store.list()){
    if(!terminal(task.status)||task.cleaned_at||!task.finished_at||Date.now()-task.finished_at<config.TASK_RETENTION_DAYS*86400000)continue;
    if(store.db.prepare("SELECT 1 FROM delivery_outbox WHERE task_id=? AND status!='DELIVERED' LIMIT 1").get(task.id))continue;
    if((!task.workspace_path||!task.thread_id)&&!store.inputs(task.id).length)continue;
    try{
      if(task.workspace_path&&task.thread_id){
        const target=path.resolve(task.workspace_path),actual=await fs.realpath(target);
        const session=task.session_id?store.session(task.session_id):undefined;
        if(session){
          if(!session.workspace_path||actual!==target||actual!==taskWorkspace(await fs.realpath(session.workspace_path),task.id))throw Error('目录不满足会话任务所有权检查，跳过清理');
          // A user session stays open; only this message's output directory expires.
          await fs.rm(actual,{recursive:true,force:false});
        }else{
          if(actual!==target||path.basename(target)!=='outputs'||path.basename(path.dirname(target))!==`lark-${task.id}`)throw Error('目录不满足机器人所有权检查，跳过清理');
          await desktop.archive(task.thread_id);await fs.rm(actual,{recursive:true,force:false});
        }
      }
      await removeInputs(task.id);
      store.db.prepare('UPDATE task_generated_images SET base64=NULL WHERE task_id=?').run(task.id);
      store.update(task.id,{cleaned_at:Date.now()});store.event(task.id,'CLEANED',{});
    }catch(e){log.warn({taskId:task.id,error:safeError(e)},'保留策略清理已跳过');}
  }
}
