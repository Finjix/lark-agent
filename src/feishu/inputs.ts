import fs from 'node:fs/promises';
import path from 'node:path';
import {randomUUID} from 'node:crypto';
import {Store} from '../storage/database.js';
import type {IncomingAttachment,Task} from '../types.js';

export interface ResourceDownloader {
  downloadResource(messageId:string,attachment:IncomingAttachment,destination:string):Promise<{size:number;contentType:string}>;
  getUserName?(userId:string):Promise<string|undefined>;
}
export {sessionInputs} from '../layout.js';
/** Attachments keep the name they were sent with, cleaned so they cannot leave the folder. */
export function inputFilename(name:string){
  return path.win32.basename(name).replace(/[<>:"/\\|?*\x00-\x1f]/g,'_').replace(/[. ]+$/,'').slice(0,140)||'attachment';
}
/** A name already taken by an earlier file gets " (2)", " (3)" ... before the extension. */
async function freeName(directory:string,name:string){
  const {name:stem,ext}=path.parse(name);let candidate=name;
  for(let n=2;;n++){
    try{await fs.lstat(path.join(directory,candidate));}catch(e:any){if(e.code==='ENOENT')return candidate;throw e;}
    candidate=`${stem} (${n})${ext}`;
  }
}
export async function prepareInputs(store:Store,task:Task,downloader:ResourceDownloader,root:string){
  const inputs=store.inputs(task.id);if(!inputs.length)return;
  await fs.mkdir(root,{recursive:true});const directory=await fs.realpath(root);
  for(const input of inputs){
    if(input.path){
      const actual=await fs.realpath(input.path),stat=await fs.stat(actual);
      if(path.dirname(actual)!==directory||!stat.isFile()||stat.size!==input.size)throw Error('已接收的附件不可用，请重新发送');
      continue;
    }
    const temporary=path.join(directory,`.${randomUUID()}.part`);
    try{
      const downloaded=await downloader.downloadResource(task.source_message_id,input,temporary);
      let name=inputFilename(input.name);
      if(input.kind==='image'){
        const extension=({'image/jpeg':'.jpg','image/png':'.png','image/gif':'.gif','image/webp':'.webp'} as Record<string,string>)[downloaded.contentType.split(';')[0].trim()];
        if(extension)name=name.replace(/\.[^.]+$/,'')+extension;
      }
      const destination=path.join(directory,await freeName(directory,name));
      await fs.rename(temporary,destination);store.inputReady(task.id,input.ordinal,destination,downloaded.size);
    }finally{await fs.rm(temporary,{force:true});}
  }
}
/** Deletes the attachment files received for a task. Only files directly inside the workspace's input/ are ever touched. */
export async function removeInputs(files:string[],workspace:string){
  let root:string;try{root=await fs.realpath(workspace);}catch(e:any){if(e.code==='ENOENT')return;throw e;}
  for(const file of files){
    let actual:string;try{actual=await fs.realpath(file);}catch(e:any){if(e.code==='ENOENT')continue;throw e;}
    const relative=path.relative(root,actual),parts=relative.split(path.sep);
    if(path.isAbsolute(relative)||parts.length!==2||parts[0]!=='input')throw Error('附件不属于当前会话，跳过清理');
    await fs.rm(actual,{force:false});
  }
}
