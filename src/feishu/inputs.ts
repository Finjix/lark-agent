import fs from 'node:fs/promises';
import path from 'node:path';
import {randomUUID} from 'node:crypto';
import {config} from '../config.js';
import {Store} from '../storage/database.js';
import type {IncomingAttachment,Task} from '../types.js';

export interface ResourceDownloader {
  downloadResource(messageId:string,attachment:IncomingAttachment,destination:string):Promise<{size:number;contentType:string}>;
}
export const inputRoot=path.join(config.data,'inputs');
export function inputDirectory(taskId:string,root=inputRoot){
  if(!/^[0-9a-f]{8}-[0-9a-f-]{27}$/.test(taskId))throw Error('无效的附件任务标识');
  return path.join(root,taskId);
}
export function inputFilename(name:string,ordinal:number){
  const safe=path.win32.basename(name).replace(/[<>:"/\\|?*\x00-\x1f]/g,'_').replace(/[. ]+$/,'').slice(0,140)||'attachment';
  return `${ordinal+1}-${safe}`;
}
async function ownedDirectory(taskId:string,root:string,create=false){
  const directory=inputDirectory(taskId,root);
  if(create)await fs.mkdir(directory,{recursive:true});
  const actualRoot=await fs.realpath(root),actual=await fs.realpath(directory);
  if(actual!==path.join(actualRoot,taskId))throw Error('附件目录不属于当前任务');
  return actual;
}
export async function prepareInputs(store:Store,task:Task,downloader:ResourceDownloader,root=inputRoot){
  const inputs=store.inputs(task.id);if(!inputs.length)return;
  const directory=await ownedDirectory(task.id,root,true);
  for(const input of inputs){
    if(input.path){
      const actual=await fs.realpath(input.path),stat=await fs.stat(actual);
      if(path.dirname(actual)!==directory||!stat.isFile()||stat.size!==input.size)throw Error('已接收的附件不可用，请重新发送');
      continue;
    }
    const temporary=path.join(directory,`.${randomUUID()}.part`);
    try{
      const downloaded=await downloader.downloadResource(task.source_message_id,input,temporary);
      let name=inputFilename(input.name,input.ordinal);
      if(input.kind==='image'){
        const extension=({'image/jpeg':'.jpg','image/png':'.png','image/gif':'.gif','image/webp':'.webp'} as Record<string,string>)[downloaded.contentType.split(';')[0].trim()];
        if(extension)name=name.replace(/\.[^.]+$/,'')+extension;
      }
      const destination=path.join(directory,name);
      try{if(!(await fs.lstat(destination)).isFile())throw Error('附件目标不是普通文件');}catch(e:any){if(e.code!=='ENOENT')throw e;}
      await fs.rename(temporary,destination);store.inputReady(task.id,input.ordinal,destination,downloaded.size);
    }finally{await fs.rm(temporary,{force:true});}
  }
}
export async function removeInputs(taskId:string,root=inputRoot){
  let directory:string;try{directory=await ownedDirectory(taskId,root);}catch(e:any){if(e.code==='ENOENT')return;throw e;}
  await fs.rm(directory,{recursive:true,force:false});
}
