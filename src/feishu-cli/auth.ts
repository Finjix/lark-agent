import fs from 'node:fs/promises';
import path from 'node:path';
import {config,restrictSecret} from '../config.js';
import {log,safeError} from '../log.js';

const ACCOUNTS='https://accounts.feishu.cn',OPEN='https://open.feishu.cn';
const REFRESH_MARGIN_MS=5*60*1000;
export interface Credentials{accessToken:string;expiresAt:number;refreshToken?:string;refreshExpiresAt?:number;scope?:string;savedAt:number}
/** Everything the Feishu CLI needs for one user lives under user/<session>/feishu-cli. */
export const stateDirectory=(root:string)=>path.join(root,'feishu-cli');
const credentialFile=(root:string)=>path.join(stateDirectory(root),'credentials.json');

export async function loadCredentials(root:string):Promise<Credentials|undefined>{
  try{return JSON.parse(await fs.readFile(credentialFile(root),'utf8'));}catch(e){if((e as NodeJS.ErrnoException).code==='ENOENT')return;throw e;}
}
async function saveCredentials(root:string,credentials:Credentials){
  await fs.mkdir(stateDirectory(root),{recursive:true});
  const file=credentialFile(root),temp=`${file}.tmp`;
  await fs.writeFile(temp,JSON.stringify(credentials),{mode:0o600});restrictSecret(temp);
  await fs.rename(temp,file);
}
export const removeCredentials=(root:string)=>fs.rm(credentialFile(root),{force:true});

async function form(url:string,fields:Record<string,string>,basic?:boolean){
  const response=await fetch(url,{method:'POST',signal:AbortSignal.timeout(20000),headers:{'content-type':'application/x-www-form-urlencoded',
    ...(basic?{authorization:`Basic ${Buffer.from(`${config.FEISHU_APP_ID}:${config.FEISHU_APP_SECRET}`).toString('base64')}`}:{})},body:new URLSearchParams(fields)});
  const text=await response.text();let body:any;try{body=JSON.parse(text);}catch{body={error:'bad_response',error_description:text.slice(0,200)};}
  return {status:response.status,body};
}
const failure=(body:any)=>String(body?.error_description||body?.error||body?.msg||'未知错误');
const tokens=(body:any,previous?:Credentials):Credentials=>{
  const now=Date.now();
  return {accessToken:body.access_token,expiresAt:now+Number(body.expires_in??7200)*1000,
    refreshToken:body.refresh_token??previous?.refreshToken,
    refreshExpiresAt:body.refresh_token?now+Number(body.refresh_token_expires_in??604800)*1000:previous?.refreshExpiresAt,
    scope:body.scope??previous?.scope,savedAt:now};
};

interface Pending{url:string;userCode:string;expiresAt:number;abort:AbortController}
export class FeishuCliAuth {
  private pending=new Map<string,Pending>();private refreshing=new Map<string,Promise<Credentials|undefined>>();
  /** Starts the OAuth device flow (RFC 8628) and polls in the background; returns the text Codex relays to the user. */
  async login(root:string):Promise<string>{
    const existing=this.pending.get(root);
    if(existing&&existing.expiresAt>Date.now())return this.loginText(existing);
    existing?.abort.abort();
    const scope=['offline_access',...config.FEISHU_CLI_SCOPES.filter(x=>x!=='offline_access')].join(' ');
    const {status,body}=await form(`${ACCOUNTS}/oauth/v1/device_authorization`,{client_id:config.FEISHU_APP_ID,scope},true);
    if(status>=400||body.error||!body.device_code)throw Error(`发起飞书授权失败：${failure(body)}（检查应用是否已开通并发布所需权限，见 FEISHU_CLI_SCOPES）`);
    const lifetime=Number(body.expires_in??240)*1000;
    const pending:Pending={url:body.verification_uri_complete||body.verification_uri,userCode:String(body.user_code??''),expiresAt:Date.now()+lifetime,abort:new AbortController()};
    this.pending.set(root,pending);
    void this.poll(root,pending,String(body.device_code),Math.max(1,Number(body.interval??5))).catch(e=>log.warn({error:safeError(e)},'飞书授权轮询失败'))
      .finally(()=>{if(this.pending.get(root)===pending)this.pending.delete(root);});
    return this.loginText(pending);
  }
  private loginText(p:Pending){
    return `请让用户在 ${Math.ceil((p.expiresAt-Date.now())/60000)} 分钟内用自己的飞书账号打开下面的链接完成授权${p.userCode?`（授权码 ${p.userCode}）`:''}：\n${p.url}\n授权完成后凭据会自动保存，用户回复后再重试原任务；本次回复请把链接发给用户并结束。`;
  }
  private async poll(root:string,pending:Pending,deviceCode:string,interval:number){
    while(Date.now()<pending.expiresAt&&!pending.abort.signal.aborted){
      await new Promise(resolve=>setTimeout(resolve,interval*1000));
      if(pending.abort.signal.aborted)return;
      let result;try{result=await form(`${OPEN}/open-apis/authen/v2/oauth/token`,{grant_type:'urn:ietf:params:oauth:grant-type:device_code',device_code:deviceCode,client_id:config.FEISHU_APP_ID,client_secret:config.FEISHU_APP_SECRET});}
      catch{interval=Math.min(interval+1,60);continue;}
      const {body}=result;
      if(body.access_token){await saveCredentials(root,tokens(body));log.info('用户飞书 CLI 授权完成');return;}
      if(body.error==='authorization_pending')continue;
      if(body.error==='slow_down'){interval=Math.min(interval+5,60);continue;}
      log.warn({reason:body.error},'飞书授权未完成');return;
    }
  }
  async status(root:string):Promise<string>{
    const credentials=await loadCredentials(root);
    if(!credentials){const p=this.pending.get(root);return p&&p.expiresAt>Date.now()?'未登录：已发起授权，等待用户在飞书中确认':'未登录。运行 auth login 获取授权链接';}
    const live=credentials.refreshExpiresAt?credentials.refreshExpiresAt>Date.now():credentials.expiresAt>Date.now();
    return live?`已登录${credentials.scope?`，权限：${credentials.scope}`:''}\n登录状态有效期至：${new Date(credentials.refreshExpiresAt??credentials.expiresAt).toISOString()}`:'登录已过期。运行 auth login 重新授权';
  }
  async logout(root:string){this.pending.get(root)?.abort.abort();this.pending.delete(root);await removeCredentials(root);return '已退出登录并删除本地凭据';}
  /** A fresh user access token, refreshing (and rotating the refresh token) when it is about to expire. */
  token(root:string):Promise<string|undefined>{
    const running=this.refreshing.get(root);if(running)return running.then(c=>c?.accessToken);
    const next=this.fresh(root).finally(()=>this.refreshing.delete(root));this.refreshing.set(root,next);
    return next.then(c=>c?.accessToken);
  }
  private async fresh(root:string):Promise<Credentials|undefined>{
    const current=await loadCredentials(root);
    if(!current)return;
    if(current.expiresAt-Date.now()>REFRESH_MARGIN_MS)return current;
    if(!current.refreshToken||(current.refreshExpiresAt&&current.refreshExpiresAt<=Date.now())){await removeCredentials(root);return;}
    const {status,body}=await form(`${OPEN}/open-apis/authen/v2/oauth/token`,{grant_type:'refresh_token',client_id:config.FEISHU_APP_ID,client_secret:config.FEISHU_APP_SECRET,refresh_token:current.refreshToken});
    if(body.access_token){const next=tokens(body,current);await saveCredentials(root,next);return next;}
    // A rejected refresh token can never work again; anything else (network, rate limit) may succeed next time.
    if(status<500&&['invalid_grant','invalid_client'].includes(body.error)){await removeCredentials(root);return;}
    throw Error(`刷新飞书登录失败：${failure(body)}`);
  }
}
