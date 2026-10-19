import fs from 'node:fs/promises';
import path from 'node:path';
import {randomUUID} from 'node:crypto';
import {config} from '../config.js';
import type {GeneratedImage,Task} from '../types.js';

const maxBytes=30*1024*1024;
const pngSignature=Buffer.from([137,80,78,71,13,10,26,10]);
const SLACK_MS=2000;
const safeId=/^[a-zA-Z0-9_-]{1,150}$/;
/**
 * Codex saves an image it generates in <home>/generated_images/<thread>/<id>.png and `codex exec` does not list it among its events,
 * so the images of a task are the ones that appeared in its thread's folder while it ran.
 */
export async function generatedImages(threadId:string,since:number,codexHome=config.codexHome):Promise<GeneratedImage[]>{
  if(!safeId.test(threadId))return [];
  const directory=path.join(codexHome,'generated_images',threadId);
  let names:string[];try{names=await fs.readdir(directory);}catch(e:any){if(e.code==='ENOENT')return [];throw e;}
  const found:GeneratedImage[]=[];
  for(const name of names.sort()){
    const id=name.replace(/\.png$/i,'');if(id===name||!safeId.test(id))continue;
    const file=path.join(directory,name),stat=await fs.stat(file).catch(()=>undefined);
    if(stat?.isFile()&&stat.mtimeMs>=since-SLACK_MS)found.push({id,savedPath:file});
  }
  return found;
}
export async function stageGeneratedImage(task:Task,image:GeneratedImage,codexHome=config.codexHome){
  if(!task.workspace_path||!task.thread_id)throw Error('生成图片缺少任务目录或会话关联');
  if(!safeId.test(image.id)||!safeId.test(task.thread_id))throw Error('生成图片标识无效');
  // Only the exact image of the task's own thread may leave Codex's image folder.
  const expected=path.join(await fs.realpath(codexHome),'generated_images',task.thread_id,`${image.id}.png`);
  if(path.relative(expected,path.resolve(image.savedPath))!=='')throw Error('生成图片路径不属于当前会话和图片项');
  const actual=await fs.realpath(image.savedPath);
  if(path.relative(expected,actual)!=='')throw Error('生成图片路径包含越界链接');
  const stat=await fs.stat(actual);
  if(!stat.isFile()||stat.size>maxBytes)throw Error('生成图片不是文件或超过 30 MB 上限');
  const data=await fs.readFile(actual);
  if(data.length>maxBytes||!data.subarray(0,8).equals(pngSignature))throw Error('生成图片不是有效的 PNG 数据或超过 30 MB 上限');
  await fs.mkdir(task.workspace_path,{recursive:true});
  const root=await fs.realpath(task.workspace_path),destination=path.join(root,`codex-image-${image.id}.png`);
  const temporary=path.join(root,`.image-${randomUUID()}.tmp`);
  // Atomic replacement also repairs a partial staging file left by an interrupted process.
  try{await fs.writeFile(temporary,data,{flag:'wx'});await fs.rename(temporary,destination);}
  finally{await fs.rm(temporary,{force:true});}
  return destination;
}
