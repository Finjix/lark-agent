import {spawn,execFile} from 'node:child_process';
import {promisify} from 'node:util';
import fs from 'node:fs/promises';
import {existsSync} from 'node:fs';
import path from 'node:path';
import {config} from '../config.js';
import {FeishuCliAuth,stateDirectory} from './auth.js';

const TIMEOUT_MS=120000,OUTPUT_LIMIT=256*1024;
export interface CliResult{code:number;stdout:string;stderr:string}
/** The CLI shipped in this project's node_modules (FEISHU_CLI_PATH overrides it). */
export const feishuCliBinary=()=>config.FEISHU_CLI_PATH||path.join(config.root,'node_modules','@larksuite','cli','bin',process.platform==='win32'?'lark-cli.exe':'lark-cli');
const note=(text:string):CliResult=>({code:1,stdout:'',stderr:text});
const exec=promisify(execFile);
const QR_FILE='feishu-login-qr.png';
/** `auth login` accepts only --scope (permission names, comma or space separated); anything else would ask for what this service does not manage. */
export function loginScopes(args:string[]):string[]{
  const scopes:string[]=[];
  for(let i=0;i<args.length;i++){
    const arg=args[i],inline=arg.startsWith('--scope=');
    if(arg!=='--scope'&&!inline)throw Error('auth login 只支持 --scope <权限名>（逗号分隔）。缺少哪项权限，命令的报错里会写明权限名。');
    const value=inline?arg.slice(8):args[++i];
    for(const scope of (value??'').split(/[\s,]+/).filter(Boolean)){
      if(!/^[A-Za-z0-9_.:-]{1,100}$/.test(scope))throw Error(`权限名不合法：${scope}`);
      scopes.push(scope);
    }
    if(!value)throw Error('--scope 后面需要权限名');
  }
  return scopes;
}
// Only what a console program needs; the service's own secrets and the user profile never reach the child.
const inherited=['SystemRoot','windir','ComSpec','PATHEXT','PATH','Path','TEMP','TMP'];

/** A QR image of the authorization link, saved in the task's delivery folder so it goes out with the reply (scanning beats typing a link on a phone). */
async function loginQr(url:string|undefined,workdir:string,state:string){
  const binary=feishuCliBinary();if(!url||!existsSync(binary))return;
  const file=path.join(workdir,QR_FILE);
  try{
    await fs.mkdir(workdir,{recursive:true});await fs.mkdir(path.join(state,'config'),{recursive:true});await fs.rm(file,{force:true});
    const env:Record<string,string>={LARKSUITE_CLI_CONFIG_DIR:path.join(state,'config'),LARKSUITE_CLI_NO_UPDATE_NOTIFIER:'1'};
    for(const key of inherited)if(process.env[key])env[key]=process.env[key]!;
    await exec(binary,['auth','qrcode',url,'-o',QR_FILE,'--size','320'],{cwd:workdir,env,timeout:15000,windowsHide:true});
    return (await fs.stat(file)).size>0?file:undefined;
  }catch{return;}
}

/**
 * Runs the official lark-cli for one user session. The CLI's own login store is a machine-wide keychain (on Windows the registry),
 * so credentials are injected through the environment from user/<session>/feishu-cli instead, and `auth` is handled here.
 */
export async function runFeishuCli(auth:FeishuCliAuth,root:string,args:string[],workdir:string=root):Promise<CliResult>{
  if(!config.FEISHU_APP_ID||!config.FEISHU_APP_SECRET)return note('服务未配置飞书应用凭证');
  const [first,second]=args;
  if(first==='auth'){
    try{
      if(second==='login'){
        const text=await auth.login(root,loginScopes(args.slice(2))),qr=await loginQr(auth.loginUrl(root),workdir,stateDirectory(root));
        return {code:0,stdout:`${text}\n${qr?`二维码已生成并保存为 ${qr}，会随本次回复自动发给用户，用户用飞书扫码即可授权；回复里同时附上上面的链接。\n`:''}`,stderr:''};
      }
      if(second==='status')return {code:0,stdout:await auth.status(root)+'\n',stderr:''};
      if(second==='logout')return {code:0,stdout:await auth.logout(root)+'\n',stderr:''};
    }catch(e){return note((e as Error).message);}
    return note('auth 只支持：login、status、logout');
  }
  if(first==='config'||args.includes('--profile'))return note('配置由服务管理，不能通过 CLI 修改');
  const binary=feishuCliBinary();
  if(!existsSync(binary))return note('飞书 CLI 未安装，请管理员在项目目录运行 npm ci');
  let token;try{token=await auth.token(root);}catch(e){return note((e as Error).message);}
  if(!token)return note('该用户尚未登录飞书 CLI。先运行 auth login，把授权链接发给用户，等用户授权后再重试');
  const state=stateDirectory(root),home=path.join(state,'home');
  for(const directory of [path.join(state,'config'),path.join(state,'logs'),home])await fs.mkdir(directory,{recursive:true});
  const env:Record<string,string>={USERPROFILE:home,HOME:home,
    LARKSUITE_CLI_APP_ID:config.FEISHU_APP_ID,LARKSUITE_CLI_APP_SECRET:config.FEISHU_APP_SECRET,LARKSUITE_CLI_USER_ACCESS_TOKEN:token,LARKSUITE_CLI_BRAND:'feishu',
    // User identity only: acting as the bot would use the app's permissions instead of this user's own.
    LARKSUITE_CLI_STRICT_MODE:'user',LARKSUITE_CLI_DEFAULT_AS:'user',LARKSUITE_CLI_CONFIG_DIR:path.join(state,'config'),LARKSUITE_CLI_LOG_DIR:path.join(state,'logs'),
    LARKSUITE_CLI_NO_UPDATE_NOTIFIER:'1',LARKSUITE_CLI_NO_SKILLS_NOTIFIER:'1'};
  for(const key of inherited)if(process.env[key])env[key]=process.env[key]!;
  // Relative file arguments resolve inside the task's output directory.
  const created=!existsSync(workdir);await fs.mkdir(workdir,{recursive:true});
  return new Promise(resolve=>{
    const child=spawn(binary,args,{cwd:workdir,env,windowsHide:true,stdio:['ignore','pipe','pipe']});
    let stdout='',stderr='',done=false;
    const finish=(result:CliResult)=>{
      if(done)return;done=true;clearTimeout(timer);
      // rmdir only removes an empty directory, so a task that saved something keeps its folder.
      (created?fs.rmdir(workdir).catch(()=>{}):Promise.resolve()).then(()=>resolve(result));
    };
    const timer=setTimeout(()=>{child.kill();finish({code:124,stdout,stderr:`${stderr}\n命令超时（${TIMEOUT_MS/1000} 秒）`});},TIMEOUT_MS);
    child.stdout.on('data',d=>{if(stdout.length<OUTPUT_LIMIT)stdout+=d;});child.stderr.on('data',d=>{if(stderr.length<OUTPUT_LIMIT)stderr+=d;});
    child.on('error',e=>finish(note(`无法启动飞书 CLI：${e.message}`)));
    child.on('close',code=>finish({code:code??1,stdout,stderr}));
  });
}
