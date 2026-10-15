import * as lark from '@larksuiteoapi/node-sdk';
import {createReadStream,createWriteStream} from 'node:fs';
import {Transform} from 'node:stream';
import {pipeline} from 'node:stream/promises';
import {config} from '../config.js';
import {log,safeError} from '../log.js';
import type {IncomingMessage,IncomingAttachment} from '../types.js';
import {incomingMessage} from './incoming.js';

export const MAX_INPUT_BYTES=30*1024*1024;

export class FeishuError extends Error {constructor(message:string,public code:number){super(message);}}
export function checked(response:any){
  if(response?.code&&response.code!==0)throw new FeishuError(String(response.msg??'飞书请求失败'),response.code);
  return response?.data??response;
}
export function sanitizedFeishuError(error:any){
  if(error instanceof FeishuError)return error;
  const data=error?.response?.data,fields=(data?.error?.field_violations??[]).map((x:any)=>`${x.field}: ${x.description}`).join('; ');
  return new FeishuError(safeError(new Error([data?.msg??error?.message??'飞书请求失败',fields].filter(Boolean).join(' — '))),typeof data?.code==='number'?data.code:0);
}
export async function downloadError(error:any){
  const body=error?.response?.data;
  let parsed=body;
  if(typeof body?.[Symbol.asyncIterator]==='function'){
    const chunks:Buffer[]=[];let size=0;
    const timer=setTimeout(()=>body.destroy?.(),3000);timer.unref();
    try{
      for await(const chunk of body){size+=chunk.length;if(size>65536){body.destroy?.();break;}chunks.push(Buffer.from(chunk));}
      parsed=JSON.parse(Buffer.concat(chunks).toString('utf8'));
    }catch{parsed=undefined;}finally{clearTimeout(timer);}
  }
  const result=sanitizedFeishuError({message:error?.message,response:{data:parsed}});
  if(result.code===99991672&&/im:message/.test(result.message))return new FeishuError('下载用户附件需要应用身份权限 im:message:readonly，请在飞书开发者后台开通并发布新版本；如需审批，须完成审批。',result.code);
  return result;
}
export class FeishuClient {
  sdk:lark.Client; ws?:lark.WSClient; connected=false; botId='';lastError='';
  private appId:string; private appSecret:string;
  private logger={
    error:(...args:any[])=>{this.updateWsState(args);this.lastError='飞书 SDK 连接或请求异常';log.warn('飞书 SDK 报告连接或请求异常（详情已脱敏）');},
    warn:(...args:any[])=>{this.updateWsState(args);},info:(...args:any[])=>{this.updateWsState(args);},debug:(..._args:any[])=>{},trace:(..._args:any[])=>{},
  };
  private updateWsState(args:any[]){
    const text=args.flat(Infinity).filter(x=>typeof x==='string').join(' ');
    if(!/\bws\b|websocket/i.test(text))return;
    if(/disconnected|closed|reconnecting|connect.*fail|connect.*error/i.test(text))this.connected=false;
    else if(/connect.*success|\bconnected\b|client ready/i.test(text)){this.connected=true;this.lastError='';}
  }
  constructor(credentials={appId:config.FEISHU_APP_ID,appSecret:config.FEISHU_APP_SECRET}){
    this.appId=credentials.appId;this.appSecret=credentials.appSecret;lark.defaultHttpInstance.defaults.timeout=30000;
    this.sdk=new lark.Client({appId:this.appId,appSecret:this.appSecret,appType:lark.AppType.SelfBuild,domain:lark.Domain.Feishu,logger:this.logger});
  }
  private async protect<T>(fn:()=>Promise<T>){try{return checked(await fn());}catch(e){throw sanitizedFeishuError(e);}}
  async raw(method:string,url:string,data?:any){return this.protect(()=>this.sdk.request({method,url:`https://open.feishu.cn${url}`,data}));}
  async start(receive:(message:IncomingMessage)=>Promise<void>){
    if(!this.appId||!this.appSecret)throw Error('请先在项目 .env 中配置飞书 App ID 和 App Secret');
    const info=await this.raw('GET','/open-apis/bot/v3/info');this.botId=info.bot?.open_id??info.open_id??'';
    this.ws=new lark.WSClient({appId:this.appId,appSecret:this.appSecret,domain:lark.Domain.Feishu,logger:this.logger});
    await this.ws.start({eventDispatcher:new lark.EventDispatcher({}).register({'im.message.receive_v1':async(event:any)=>{
      const message=incomingMessage(event,this.botId);if(!message)return;
      // Let persistence errors reject: SDK may retry, while the unique message key prevents duplicates.
      await receive(message);
    }})});
    this.connected=true;this.lastError='';
  }
  async reply(messageId:string,chatId:string,type:string,content:unknown,uuid:string){
    if(messageId)return this.protect(()=>this.sdk.im.message.reply({path:{message_id:messageId},data:{msg_type:type,content:JSON.stringify(content),uuid,reply_in_thread:false}}));
    return this.protect(()=>this.sdk.im.message.create({params:{receive_id_type:'chat_id'},data:{receive_id:chatId,msg_type:type,content:JSON.stringify(content),uuid}}));
  }
  async upload(file:string,name:string){return this.protect(()=>this.sdk.im.file.create({data:{file_type:'stream',file_name:name,file:createReadStream(file)}}));}
  async downloadResource(messageId:string,attachment:IncomingAttachment,destination:string){
    try{
      const resource=await this.sdk.im.messageResource.get({path:{message_id:messageId,file_key:attachment.key},params:{type:attachment.kind}});
      const stream=resource.getReadableStream();
      if(Number(resource.headers['content-length'])>MAX_INPUT_BYTES){stream.destroy();throw Error('附件超过 30 MB 接收上限');}
      let size=0;
      const limit=new Transform({transform(chunk:Buffer,_encoding,callback){size+=chunk.length;callback(size>MAX_INPUT_BYTES?Error('附件超过 30 MB 接收上限'):null,chunk);}});
      await pipeline(stream,limit,createWriteStream(destination,{flags:'wx'}),{signal:AbortSignal.timeout(60000)});
      return {size,contentType:String(resource.headers['content-type']??'')};
    }catch(e){throw await downloadError(e);}
  }
  close(){this.connected=false;this.ws?.close({force:true});}
}
