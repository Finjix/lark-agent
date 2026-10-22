import {spawn,type ChildProcessWithoutNullStreams} from 'node:child_process';
import {createRequire} from 'node:module';
import {statSync} from 'node:fs';
import path from 'node:path';
import readline from 'node:readline';

/** What `codex app-server` sends of its own accord: a method and its params. */
export interface Notification {method:string;params:any}
/** A request the app-server refused, with its JSON-RPC error code ("no active turn to steer" is -32600). */
export class AppServerError extends Error {
  constructor(message:string,readonly code?:number){super(message);this.name='AppServerError';}
}
/**
 * One `codex app-server` process, spoken to in JSON-RPC over its stdin and stdout, one message per line. What it sends of its own accord is kept
 * from the start, in order, and read once through `notifications`, which ends when the process does (`exitReason` then says why, unless it was
 * closed). A request of the server's own (an approval, a question for the user) cannot be answered by anyone here and is declined.
 */
export interface AppServer {
  request(method:string,params?:unknown):Promise<any>;
  notify(method:string,params?:unknown):void;
  readonly notifications:AsyncIterator<Notification>;
  readonly exitReason:string|undefined;
  close():void;
}
export interface ServerOptions {args:string[];env:Record<string,string>;signal:AbortSignal}

const TRIPLES:Record<string,string>={'win32-x64':'x86_64-pc-windows-msvc','win32-arm64':'aarch64-pc-windows-msvc','linux-x64':'x86_64-unknown-linux-musl',
  'linux-arm64':'aarch64-unknown-linux-musl','darwin-x64':'x86_64-apple-darwin','darwin-arm64':'aarch64-apple-darwin'};
/** How long after Codex exits its output may stay open before the stream is ended regardless. */
const EXIT_DRAIN_MS=1000;
const isFile=(file:string)=>{try{return statSync(file).isFile();}catch{return false;}};
const isDirectory=(folder:string)=>{try{return statSync(folder).isDirectory();}catch{return false;}};
/**
 * The native Codex binary of this platform and the folders of the tools it ships with, found as the Codex SDK finds them: through the platform
 * package of @openai/codex. It is started directly, not through the package's node launcher, so that stopping a turn stops Codex itself.
 */
export function codexBinary():{file:string;pathDirs:string[]}{
  const platform=`${process.platform}-${process.arch}`,triple=TRIPLES[platform];
  if(!triple)throw Error(`Codex 不支持这个平台：${platform}`);
  const codex=createRequire(createRequire(import.meta.url).resolve('@openai/codex/package.json'));
  const root=path.join(path.dirname(codex.resolve(`@openai/codex-${platform}/package.json`)),'vendor',triple),exe=process.platform==='win32'?'codex.exe':'codex';
  if(isFile(path.join(root,'bin',exe))&&isFile(path.join(root,'codex-package.json')))return {file:path.join(root,'bin',exe),pathDirs:[path.join(root,'codex-path')].filter(isDirectory)};
  if(isFile(path.join(root,'codex',exe)))return {file:path.join(root,'codex',exe),pathDirs:[path.join(root,'path')].filter(isDirectory)};
  throw Error(`找不到 ${triple} 的 Codex 程序，请重新安装依赖（npm ci）`);
}
/** The environment with these folders in front of PATH (Windows spells the variable any way; one spelling is kept, as the SDK does). */
export function withPath(env:Record<string,string>,folders:string[],platform=process.platform):Record<string,string>{
  if(!folders.length)return env;
  const result={...env},keys=Object.keys(result).filter(k=>k.toLowerCase()==='path');
  const key=platform!=='win32'?'PATH':keys.includes('Path')?'Path':keys.at(-1)??'PATH';
  if(platform==='win32')for(const other of keys)if(other!==key)delete result[other];
  result[key]=[...folders,...(result[key]??'').split(path.delimiter).filter(entry=>entry&&!folders.includes(entry))].join(path.delimiter);
  return result;
}

export function spawnAppServer({args,env,signal}:ServerOptions):AppServer{
  const {file,pathDirs}=codexBinary();
  return serverOver(spawn(file,['app-server',...args],{env:withPath(env,pathDirs),signal,windowsHide:true,stdio:['pipe','pipe','pipe']}));
}
/** The JSON-RPC side of an app-server process already started (in the tests, a stand-in for Codex). */
export function serverOver(child:ChildProcessWithoutNullStreams):AppServer{
  let nextId=1,stderr='',ended=false,closed=false,exitReason:string|undefined,failure:Error|undefined;
  const pending=new Map<number,{resolve:(value:any)=>void;reject:(error:Error)=>void}>();
  const queue:Notification[]=[],waiting:((result:IteratorResult<Notification>)=>void)[]=[];
  const write=(message:unknown)=>{if(!ended&&child.stdin.writable)child.stdin.write(`${JSON.stringify(message)}\n`);};
  const gone=()=>failure??Error(exitReason??'Codex app-server 已关闭');
  const end=()=>{
    if(ended)return;ended=true;
    for(const request of pending.values())request.reject(gone());pending.clear();
    for(const reader of waiting.splice(0))reader({done:true,value:undefined});
  };
  child.stderr.on('data',chunk=>{stderr=(stderr+String(chunk)).slice(-4000);});
  // Writing to a process that has just gone fails with EPIPE; its exit says what happened.
  child.stdin.on('error',()=>{});
  // A stopped turn (the abort signal) and a process that could not be started end here; requests still waiting fail with the reason.
  child.once('error',error=>{failure=error;end();});
  child.once('exit',(code,how)=>{
    if(!closed)exitReason=`Codex app-server 退出了（${how?`signal ${how}`:`code ${code}`}）${stderr.trim()?`：${stderr.trim()}`:''}`;
    // A process a command of the turn left running can hold the output open long after Codex is gone (a browser waiting for a QR scan, say).
    // What Codex wrote is read within moments of its exit, so the stream ends then anyway, and requests still waiting fail.
    setTimeout(end,EXIT_DRAIN_MS).unref();
  });
  const lines=readline.createInterface({input:child.stdout,crlfDelay:Infinity});
  lines.on('line',line=>{
    let message:any;try{message=JSON.parse(line);}catch{return;}
    if(message?.id!==undefined&&message.method===undefined){
      const request=pending.get(message.id);if(!request)return;pending.delete(message.id);
      if(message.error)request.reject(new AppServerError(String(message.error.message??'Codex 拒绝了请求'),message.error.code));else request.resolve(message.result);
      return;
    }
    if(message?.id!==undefined){write({id:message.id,error:{code:-32601,message:'lark-agent 不处理这个请求'}});return;}
    if(typeof message?.method!=='string')return;
    const notification={method:message.method,params:message.params},reader=waiting.shift();
    if(reader)reader({done:false,value:notification});else queue.push(notification);
  });
  // Everything the process wrote has been read once its output closes: only then is the stream over.
  lines.once('close',()=>{exitReason??=closed?undefined:`Codex app-server 退出了${stderr.trim()?`：${stderr.trim()}`:''}`;end();});
  return {
    request:(method,params)=>new Promise((resolve,reject)=>{
      if(ended)return reject(gone());
      const id=nextId++;pending.set(id,{resolve,reject});write({id,method,...(params===undefined?{}:{params})});
    }),
    notify:(method,params)=>write({method,...(params===undefined?{}:{params})}),
    notifications:{next:()=>queue.length?Promise.resolve({done:false,value:queue.shift()!}):ended?Promise.resolve({done:true,value:undefined}):new Promise(resolve=>waiting.push(resolve))},
    get exitReason(){return exitReason;},
    // Nothing is read after this: a request still waiting (a steer sent just as the turn ended) fails now, and so does any made later.
    close:()=>{closed=true;try{child.stdin.end();}catch{}if(child.exitCode===null&&child.signalCode===null)child.kill();end();},
  };
}
