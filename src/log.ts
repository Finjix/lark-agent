import {mkdirSync,createWriteStream,type WriteStream} from 'node:fs';
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
const levels={debug:20,info:30,warn:40,error:50};
const threshold=levels[process.env.LOG_LEVEL as keyof typeof levels]??levels.info;
const secret=new Set(['secret','token','appSecret','headers','FEISHU_APP_SECRET']);
const write=(level:number,first:unknown,second?:string)=>{
  if(level<threshold)return;
  const fields=typeof first==='object'&&first!==null?first as object:{},msg=typeof first==='string'?first:second;
  let line:string;
  try{line=JSON.stringify({level,time:Date.now(),pid:process.pid,...fields,msg},(key,value)=>secret.has(key)?'[REDACTED]':value);}
  catch{line=JSON.stringify({level,time:Date.now(),pid:process.pid,msg});}
  process.stdout.write(line+'\n');file.write(line+'\n');
};
export const log = {
  debug:(first:unknown,msg?:string)=>write(levels.debug,first,msg), info:(first:unknown,msg?:string)=>write(levels.info,first,msg),
  warn:(first:unknown,msg?:string)=>write(levels.warn,first,msg), error:(first:unknown,msg?:string)=>write(levels.error,first,msg),
};
export const safeError = (error:unknown) => {
  let text=error instanceof Error?error.message:'未知错误';text=text.replace(/(Bearer\s+)[^\s]+/gi,'$1[REDACTED]');
  if(config.FEISHU_APP_SECRET)text=text.replaceAll(config.FEISHU_APP_SECRET,'[REDACTED]');return text;
};
