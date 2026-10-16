import type {IncomingMessage,ServerResponse} from 'node:http';
import {randomBytes,timingSafeEqual} from 'node:crypto';
import {config} from '../config.js';
import {Store} from '../storage/database.js';
import {readJson} from '../http-json.js';
import {log,safeError} from '../log.js';
import type {BrowserManager} from '../browser/manager.js';
import type {FeishuCliAuth} from '../feishu-cli/auth.js';
import {userData} from '../layout.js';
import {tools,type ToolContext} from './tools.js';

export const mcpToken=(store:Store):string=>store.meta('mcp_token')??store.meta('mcp_token',randomBytes(32).toString('hex'));
const PROTOCOL='2025-06-18';

/**
 * Identity comes from the thread metadata Codex attaches to every tools/call (_meta.threadId, x-codex-turn-metadata),
 * which the model cannot edit. The thread must belong to a user session and have a task running in it right now.
 */
export function identify(store:Store,params:any):{context?:Pick<ToolContext,'data'|'workspace'>;error?:string}{
  const meta=params?._meta??{},turn=meta['x-codex-turn-metadata']??{};
  const thread=meta.threadId??turn.thread_id;
  if(typeof thread!=='string'||!thread)return {error:'调用没有带线程标识，已拒绝'};
  if(turn.thread_id&&meta.threadId&&turn.thread_id!==meta.threadId)return {error:'线程标识不一致，已拒绝'};
  const session=store.sessionByThread(thread);
  if(!session?.workspace_path)return {error:'该线程不是飞书用户会话，工具仅供飞书任务使用'};
  const task=store.byThread(thread);
  if(!task?.workspace_path)return {error:'当前没有正在执行的飞书任务，已拒绝'};
  if(turn.turn_id&&task.turn_id&&turn.turn_id!==task.turn_id)return {error:'调用不属于当前任务的执行轮次，已拒绝'};
  return {context:{data:userData(store,session),workspace:task.workspace_path}};
}

const same=(a:string,b:string)=>{const x=Buffer.from(a),y=Buffer.from(b);return x.length===y.length&&timingSafeEqual(x,y);};
export class McpServer {
  constructor(private store:Store,private browsers:BrowserManager,private feishu:FeishuCliAuth){}
  private async call(params:any){
    const tool=tools.find(t=>t.name===params?.name&&t.enabled());
    if(!tool)return {isError:true,content:[{type:'text',text:`未知或未启用的工具 ${params?.name}`}]};
    const who=identify(this.store,params);
    if(!who.context){log.warn({tool:tool.name,reason:who.error},'MCP 调用被拒绝');return {isError:true,content:[{type:'text',text:who.error}]};}
    try{
      const result=await tool.run({...who.context,browsers:this.browsers,feishu:this.feishu},params.arguments??{});
      return {isError:result.isError===true,content:[{type:'text',text:result.text}]};
    }catch(e){return {isError:true,content:[{type:'text',text:safeError(e)}]};}
  }
  private async dispatch(message:any):Promise<object|undefined>{
    if(message?.id===undefined||message?.id===null)return;
    const ok=(result:object)=>({jsonrpc:'2.0',id:message.id,result});
    switch(message.method){
      case 'initialize':return ok({protocolVersion:typeof message.params?.protocolVersion==='string'?message.params.protocolVersion:PROTOCOL,capabilities:{tools:{}},serverInfo:{name:'lark-agent',version:'1.0.0'}});
      case 'ping':return ok({});
      case 'tools/list':return ok({tools:tools.filter(t=>t.enabled()).map(({name,description,inputSchema})=>({name,description,inputSchema}))});
      case 'tools/call':return ok(await this.call(message.params));
      default:return {jsonrpc:'2.0',id:message.id,error:{code:-32601,message:`不支持的方法 ${message.method}`}};
    }
  }
  async handle(request:IncomingMessage,response:ServerResponse){
    const reject=(status:number,headers:Record<string,string>={})=>response.writeHead(status,headers).end('{}');
    // Loopback only: a web page cannot reach this (no Origin allowed, Host must be local), and other local accounts need the token.
    if(request.headers.origin||!/^(127\.0\.0\.1|localhost)(:\d+)?$/.test(request.headers.host??''))return reject(403);
    if(!same(request.headers.authorization??'',`Bearer ${mcpToken(this.store)}`))return reject(401);
    if(request.method!=='POST')return reject(405,{Allow:'POST'});
    let body:any;try{body=await readJson(request);}catch{return reject(400);}
    const batch=Array.isArray(body),replies=(await Promise.all((batch?body:[body]).map((m:any)=>this.dispatch(m)))).filter(Boolean);
    if(!replies.length)return reject(202);
    response.writeHead(200,{'Content-Type':'application/json; charset=utf-8'}).end(JSON.stringify(batch?replies:replies[0]));
  }
}
export const mcpUrl=()=>`http://127.0.0.1:${config.HEALTH_PORT}/mcp`;
