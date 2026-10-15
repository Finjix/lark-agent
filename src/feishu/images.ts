import fs from 'node:fs/promises';
import path from 'node:path';
import {randomUUID} from 'node:crypto';
import {config} from '../config.js';
import type {GeneratedImage,Task} from '../types.js';

const maxBytes=30*1024*1024;
const pngSignature=Buffer.from([137,80,78,71,13,10,26,10]);
export async function stageGeneratedImage(task:Task,image:GeneratedImage,codexHome=config.codexHome){
  if(!task.workspace_path||!task.thread_id)throw Error('生成图片缺少任务目录或会话关联');
  if(!/^[a-zA-Z0-9_-]{1,150}$/.test(image.id)||!/^[a-zA-Z0-9_-]{1,150}$/.test(task.thread_id))throw Error('生成图片标识无效');
  let data:Buffer;
  if(image.savedPath){
    // Only the exact image item from the matched thread may leave Desktop's image cache.
    const expected=path.join(await fs.realpath(codexHome),'generated_images',task.thread_id,`${image.id}.png`);
    if(path.relative(expected,path.resolve(image.savedPath))!=='')throw Error('生成图片路径不属于当前会话和图片项');
    const actual=await fs.realpath(image.savedPath);
    if(path.relative(expected,actual)!=='')throw Error('生成图片路径包含越界链接');
    const stat=await fs.stat(actual);
    if(!stat.isFile()||stat.size>maxBytes)throw Error('生成图片不是文件或超过 30 MB 上限');
    data=await fs.readFile(actual);
  }else{
    const encoded=(image.base64??'').replace(/^data:image\/png;base64,/, '');
    if(!encoded||encoded.length>Math.ceil(maxBytes/3)*4||!/^[A-Za-z0-9+/]*={0,2}$/.test(encoded))throw Error('生成图片数据无效或超过 30 MB 上限');
    data=Buffer.from(encoded,'base64');
  }
  if(data.length>maxBytes||!data.subarray(0,8).equals(pngSignature))throw Error('生成图片不是有效的 PNG 数据或超过 30 MB 上限');
  await fs.mkdir(task.workspace_path,{recursive:true});
  const root=await fs.realpath(task.workspace_path),destination=path.join(root,`codex-image-${image.id}.png`);
  const temporary=path.join(root,`.image-${randomUUID()}.tmp`);
  // Atomic replacement also repairs a partial staging file left by an interrupted process.
  try{await fs.writeFile(temporary,data,{flag:'wx'});await fs.rename(temporary,destination);}
  finally{await fs.rm(temporary,{force:true});}
  return destination;
}
