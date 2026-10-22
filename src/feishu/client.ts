import * as lark from '@larksuiteoapi/node-sdk';
import {createReadStream,createWriteStream} from 'node:fs';
import {once} from 'node:events';
import {Transform} from 'node:stream';
import {pipeline} from 'node:stream/promises';
import {config} from '../config.js';
import {log,safeError} from '../log.js';
import {InputRejected,type IncomingMessage,type IncomingAttachment} from '../types.js';
import {incomingMessage} from './incoming.js';

/** The largest file or video a user may send (a zip of a material folder, say); it is fetched in pieces. */
export const MAX_INPUT_BYTES=2*1024*1024*1024;
/** Images come whole, and Feishu serves them up to 100 MB. */
export const MAX_IMAGE_BYTES=100*1024*1024;
/** Feishu takes at most 32 MB per Range request. */
const PIECE_BYTES=30*1024*1024,PIECE_TIMEOUT_MS=180000;
const tooLarge=(limit:number)=>new InputRejected(`附件超过 ${limit>=1024**3?`${limit/1024**3} GB`:`${limit/1024**2} MB`} 接收上限，请拆分后分别发送`);

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
  sdk:lark.Client; ws?:lark.WSClient; started=false; botId='';lastError='';
  private appId:string; private appSecret:string;
  private userNames=new Map<string,{name:string|undefined;expires:number}>();
  private userNameRequests=new Map<string,Promise<string|undefined>>();
  // The SDK's own logs are discarded (they can carry request details); connection state comes from its status API and callbacks.
  private logger={
    error:(..._args:any[])=>{this.lastError='飞书 SDK 连接或请求异常';log.warn('飞书 SDK 报告连接或请求异常（详情已脱敏）');},
    warn:(..._args:any[])=>{},info:(..._args:any[])=>{},debug:(..._args:any[])=>{},trace:(..._args:any[])=>{},
  };
  /** Live state of the long connection: idle, connecting, connected, reconnecting or failed. */
  get state(){return this.ws?.getConnectionStatus().state??'idle';}
  get connected(){return this.state==='connected';}
  constructor(credentials={appId:config.FEISHU_APP_ID,appSecret:config.FEISHU_APP_SECRET}){
    this.appId=credentials.appId;this.appSecret=credentials.appSecret;lark.defaultHttpInstance.defaults.timeout=30000;
    this.sdk=new lark.Client({appId:this.appId,appSecret:this.appSecret,appType:lark.AppType.SelfBuild,domain:lark.Domain.Feishu,logger:this.logger});
  }
  private async protect<T>(fn:()=>Promise<T>){try{return checked(await fn());}catch(e){throw sanitizedFeishuError(e);}}
  async raw(method:string,url:string,data?:any){return this.protect(()=>this.sdk.request({method,url:`https://open.feishu.cn${url}`,data}));}
  async getUserName(userId:string):Promise<string|undefined>{
    const cached=this.userNames.get(userId);if(cached&&cached.expires>Date.now())return cached.name;
    const pending=this.userNameRequests.get(userId);if(pending)return pending;
    const request=(async()=>{
      let name:string|undefined;
      try{
        const response=await this.protect(()=>this.sdk.request({method:'GET',
          url:`https://open.feishu.cn/open-apis/contact/v3/users/${encodeURIComponent(userId)}?user_id_type=open_id`,timeout:5000}));
        if(typeof response.user?.name==='string'&&response.user.name.trim())name=response.user.name.trim();
        else log.warn('用户信息未返回姓名；自动命名需要 contact:user.base:readonly 权限和对应的用户可见范围');
      }catch(e){
        log.warn({code:e instanceof FeishuError?e.code:undefined},'暂未获取用户显示名；会话保留现有名称，请检查 contact:contact.base:readonly、contact:user.base:readonly 权限和用户可见范围');
      }
      this.userNames.set(userId,{name,expires:Date.now()+(name?6*60*60*1000:60000)});return name;
    })();
    this.userNameRequests.set(userId,request);
    try{return await request;}finally{this.userNameRequests.delete(userId);}
  }
  async start(receive:(message:IncomingMessage)=>Promise<void>){
    if(!this.appId||!this.appSecret)throw Error('请先在项目 .env 中配置飞书 App ID 和 App Secret');
    const info=await this.raw('GET','/open-apis/bot/v3/info');this.botId=info.bot?.open_id??info.open_id??'';
    const recovered=()=>{this.lastError='';};
    this.ws=new lark.WSClient({appId:this.appId,appSecret:this.appSecret,domain:lark.Domain.Feishu,logger:this.logger,
      onReady:recovered,onReconnected:recovered,onError:error=>{this.lastError=safeError(error);}});
    await this.ws.start({eventDispatcher:new lark.EventDispatcher({}).register({'im.message.receive_v1':async(event:any)=>{
      const message=incomingMessage(event,this.botId);if(!message)return;
      // Let persistence errors reject: SDK may retry, while the unique message key prevents duplicates.
      await receive(message);
    }})});
    this.started=true;
  }
  async reply(messageId:string,chatId:string,type:string,content:unknown,uuid:string){
    if(messageId)return this.protect(()=>this.sdk.im.message.reply({path:{message_id:messageId},data:{msg_type:type,content:JSON.stringify(content),uuid,reply_in_thread:false}}));
    return this.protect(()=>this.sdk.im.message.create({params:{receive_id_type:'chat_id'},data:{receive_id:chatId,msg_type:type,content:JSON.stringify(content),uuid}}));
  }
  async upload(file:string,name:string){return this.protect(()=>this.sdk.im.file.create({data:{file_type:'stream',file_name:name,file:createReadStream(file)}}));}
  async uploadImage(file:string){return this.protect(()=>this.sdk.im.image.create({data:{image_type:'message',image:createReadStream(file)}}));}
  /**
   * Downloads one attachment of a message. Feishu serves a resource of 100 MB or more only in pieces (a Range of at most 32 MB each), so
   * files come in 30 MB pieces appended to one file; images cannot be fetched in pieces and come whole.
   */
  async downloadResource(messageId:string,attachment:IncomingAttachment,destination:string){
    const request={path:{message_id:messageId,file_key:attachment.key},params:{type:attachment.kind}};
    const limit=attachment.kind==='image'?MAX_IMAGE_BYTES:MAX_INPUT_BYTES;
    const out=createWriteStream(destination,{flags:'wx'});
    let size=0,contentType='';
    /** Appends one answer's body to the file, counting it against the limit and the time a piece may take. */
    const append=async(stream:NodeJS.ReadableStream&{destroy?:()=>void})=>{
      const counted=new Transform({transform(chunk:Buffer,_encoding,callback){size+=chunk.length;callback(size>limit?tooLarge(limit):null,chunk);}});
      await pipeline(stream,counted,async function*(source){for await(const chunk of source)if(!out.write(chunk))await once(out,'drain');},{signal:AbortSignal.timeout(PIECE_TIMEOUT_MS)});
    };
    try{
      if(attachment.kind==='image'){
        const resource=await this.sdk.im.messageResource.get(request);
        const stream=resource.getReadableStream();
        if(Number(resource.headers['content-length'])>limit){stream.destroy();throw tooLarge(limit);}
        contentType=String(resource.headers['content-type']??'');await append(stream);
      }else{
        for(let start=0,total=Infinity;start<total;){
          let resource;
          try{resource=await this.sdk.im.messageResource.get(request,{headers:{Range:`bytes=${start}-${start+PIECE_BYTES-1}`}});}
          catch(e){
            // A range past the end (an empty file) is an error to Feishu: the first piece is then asked for without one.
            if(start>0)throw e;
            resource=await this.sdk.im.messageResource.get(request);
          }
          const stream=resource.getReadableStream();
          contentType||=String(resource.headers['content-type']??'');
          const range=/^bytes (\d+)-(\d+)\/(\d+)$/.exec(String(resource.headers['content-range']??''));
          // An answer without a range is the whole file.
          if(!range){if(Number(resource.headers['content-length'])>limit){stream.destroy();throw tooLarge(limit);}await append(stream);break;}
          total=Number(range[3]);
          if(total>limit){stream.destroy();throw tooLarge(limit);}
          if(Number(range[1])!==start){stream.destroy();throw Error('附件分段下载的位置不符');}
          await append(stream);
          if(size!==Number(range[2])+1)throw Error('附件分段下载不完整');
          start=size;
        }
      }
      await new Promise<void>((resolve,reject)=>out.end((e?:Error|null)=>e?reject(e):resolve()));
      return {size,contentType};
    }catch(e){out.destroy();if(e instanceof InputRejected)throw e;throw await downloadError(e);}
  }
  close(){this.started=false;this.ws?.close({force:true});this.ws=undefined;}
}
