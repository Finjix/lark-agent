import * as lark from '@larksuiteoapi/node-sdk';
import {createReadStream} from 'node:fs';
import {config} from '../config.js';
import {log,safeError} from '../log.js';
import type {IncomingMessage} from '../types.js';

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
      const msg=event.message,sender=event.sender;
      if(sender?.sender_type!=='user'||!msg?.message_id)return;
      const isGroup=msg.chat_type==='group';
      if(isGroup&&(!this.botId||!msg.mentions?.some((m:any)=>m.id?.open_id===this.botId)))return;
      let text='';try{const content=JSON.parse(msg.content);if(msg.message_type==='text')text=content.text??'';}catch{return;}
      if(!text)return;
      for(const mention of msg.mentions??[])if(mention.id?.open_id===this.botId)text=text.replaceAll(mention.key,'');
      const message:IncomingMessage={tenant:event.tenant_key??sender.tenant_key??'',user:sender.sender_id?.open_id??'',chat:msg.chat_id,message:msg.message_id,text:text.trim(),isGroup};
      if(!message.tenant||!message.user)return;
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
  close(){this.connected=false;this.ws?.close({force:true});}
}
