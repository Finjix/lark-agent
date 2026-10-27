import {mkdirSync,createWriteStream,type WriteStream} from 'node:fs';
import {Worker} from 'node:worker_threads';
import path from 'node:path';
import {config} from './config.js';
const directory=path.join(config.data,'logs');mkdirSync(directory,{recursive:true});
// A new file per day and process; opened lazily so short CLI commands leave no empty file. Old files are removed by maintenance.ts.
let stream:WriteStream|undefined,day='';
const file={write:(line:string)=>{
  const today=new Date().toISOString().slice(0,10);
  if(!stream||today!==day){stream?.end();day=today;stream=createWriteStream(path.join(directory,`${today}-${process.pid}.jsonl`),{flags:'a'});}
  return stream.write(line);
}};
/**
 * The console is written from a worker thread. On Windows a write to stdout (a console window or a pipe) blocks until it is taken, so a
 * stalled console (text being selected, a hung terminal) would otherwise freeze the whole service. Lines wait for the worker instead;
 * past MAX_CONSOLE_BACKLOG they are left out of the console only (the log file has every line) and a note says how many.
 */
const MAX_CONSOLE_BACKLOG=5000;
const CONSOLE_WORKER=`const {parentPort}=require('node:worker_threads'),tty=require('node:tty'),fs=require('node:fs');
const out=tty.isatty(1)?new tty.WriteStream(1):undefined;
parentPort.on('message',line=>{try{if(out)out.write(line);else fs.writeSync(1,line);}catch{}parentPort.postMessage(0);});`;
let worker:Worker|undefined,pending=0,omitted=0;
const send=(line:string)=>{
  if(pending++===0)worker!.ref();
  worker!.postMessage(line);
};
const screen={write:(line:string)=>{
  if(!worker){
    worker=new Worker(CONSOLE_WORKER,{eval:true});
    // Held only while lines are waiting, so the process ends once its last lines are shown, and never waits on an idle worker.
    worker.on('message',()=>{if(--pending===0)worker!.unref();});
    worker.on('error',()=>{});
  }
  if(pending>=MAX_CONSOLE_BACKLOG){omitted++;return;}
  if(omitted){send(`(控制台输出曾停滞，省略了 ${omitted} 行，完整内容见 ${directory})\n`);omitted=0;}
  send(line);
}};
const levels={debug:20,info:30,warn:40,error:50};
const threshold=levels[process.env.LOG_LEVEL as keyof typeof levels]??levels.info;
const secret=new Set(['secret','token','appSecret','headers','FEISHU_APP_SECRET']);
const write=(level:number,first:unknown,second?:string)=>{
  if(level<threshold)return;
  const fields=typeof first==='object'&&first!==null?first as object:{},msg=typeof first==='string'?first:second;
  let line:string;
  try{line=JSON.stringify({level,time:Date.now(),pid:process.pid,...fields,msg},(key,value)=>secret.has(key)?'[REDACTED]':value);}
  catch{line=JSON.stringify({level,time:Date.now(),pid:process.pid,msg});}
  file.write(line+'\n');screen.write(line+'\n');
};
export const log = {
  debug:(first:unknown,msg?:string)=>write(levels.debug,first,msg), info:(first:unknown,msg?:string)=>write(levels.info,first,msg),
  warn:(first:unknown,msg?:string)=>write(levels.warn,first,msg), error:(first:unknown,msg?:string)=>write(levels.error,first,msg),
};
export const safeError = (error:unknown) => {
  let text=error instanceof Error?error.message:'未知错误';text=text.replace(/(Bearer\s+)[^\s]+/gi,'$1[REDACTED]');
  if(config.FEISHU_APP_SECRET)text=text.replaceAll(config.FEISHU_APP_SECRET,'[REDACTED]');return text;
};
