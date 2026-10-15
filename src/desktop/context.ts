import fs from 'node:fs';
import path from 'node:path';
import {randomUUID} from 'node:crypto';
import {execFileSync} from 'node:child_process';
import Database from 'better-sqlite3';
import {FramedPipe,RpcRejected} from './transport.js';
import {config} from '../config.js';
export interface CallerContext {threadId:string;turnId:string;}
export function desktopVersion(){
  if(process.platform!=='win32')throw Error('Only Windows x64 is supported');
  return execFileSync('powershell.exe',['-NoProfile','-NonInteractive','-Command',"(Get-AppxPackage OpenAI.Codex | Select-Object -First 1).Version"],{encoding:'utf8',windowsHide:true,timeout:10000}).trim();
}
export function callerCandidates(home=config.codexHome):CallerContext[]{
  const files=fs.readdirSync(home).filter(x=>/^state_\d+\.sqlite$/.test(x)).sort((a,b)=>Number(b.match(/\d+/)![0])-Number(a.match(/\d+/)![0]));
  if(!files.length)throw Error('未找到 Desktop 本地聊天索引，请先在 Desktop 创建一个本地聊天');
  const db=new Database(path.join(home,files[0]),{readonly:true,fileMustExist:true});
  try{return (db.prepare('SELECT id,rollout_path FROM threads WHERE rollout_path IS NOT NULL ORDER BY updated_at DESC LIMIT 30').all() as any[]).flatMap(row=>{
    try{
      const fd=fs.openSync(row.rollout_path,'r');let text='';
      try{const size=fs.fstatSync(fd).size,length=Math.min(size,4*1024*1024),buf=Buffer.alloc(length);fs.readSync(fd,buf,0,length,size-length);text=buf.toString('utf8');}finally{fs.closeSync(fd);}
      let turnId:string|undefined,completed:string|undefined;
      for(const line of text.split('\n')){try{const entry=JSON.parse(line);if(entry.type==='turn_context'&&entry.payload?.turn_id)turnId=entry.payload.turn_id;
        if(entry.type==='event_msg'&&entry.payload?.type==='task_complete')completed=entry.payload.turn_id??turnId;
      }catch{}}
      return completed||turnId?[{threadId:row.id,turnId:completed??turnId!}]:[];
    }catch{return [];}
  });}finally{db.close();}
}
export function parseToolResult(result:any){
  if(result?.success===false||result?.isError)throw new RpcRejected('Desktop tool rejected the request');
  for(const item of result?.contentItems??result?.content??[]){if(['inputText','text'].includes(item.type))try{return JSON.parse(item.text);}catch{}}
  throw new RpcRejected('Desktop tool returned no structured result');
}
export class AppTools extends FramedPipe {
  constructor(pipe:string,public caller:CallerContext){super(pipe,10000);}
  async call(tool:string,args:unknown,timeout=90000){
    const id=randomUUID();
    try{const r=await this.roundTrip({jsonrpc:'2.0',id,method:'tools/call',params:{namespace:'codex_app',callerSource:'codex',hostId:'local',...this.caller,tool,arguments:args,callId:randomUUID()}},timeout);
      return parseToolResult(r.result);
    }catch(e){try{this.send({jsonrpc:'2.0',id,method:'tools/cancel'});}catch{}throw e;}
  }
}
export async function discoverToolEndpoint(){
  const candidates=fs.readdirSync('\\\\.\\pipe\\').filter(x=>/^codex-browser-use-[0-9a-f-]+$/.test(x));
  const results=await Promise.allSettled(candidates.map(async name=>{
    const pipe=new FramedPipe('\\\\.\\pipe\\'+name,1500);
    try{await pipe.connect();const r=await pipe.roundTrip({jsonrpc:'2.0',id:randomUUID(),method:'tools/list',params:{threadStartKind:'all'}},1500);
      const tools=r.result?.tools??[];if(['create_thread','list_threads','read_thread','set_thread_archived'].every(name=>tools.some((t:any)=>t.namespace==='codex_app'&&t.name===name)))return pipe.path;
    }catch{}finally{pipe.close();}
  }));
  const valid=results.flatMap(r=>r.status==='fulfilled'&&r.value?[r.value]:[]);
  if(valid.length!==1)throw Error(`Desktop 工具通道识别失败：发现 ${valid.length} 个匹配端点`);
  return valid[0];
}
