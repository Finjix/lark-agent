import {fileURLToPath} from 'node:url';
import path from 'node:path';
import {mkdirSync,existsSync} from 'node:fs';
import {execFileSync} from 'node:child_process';
export const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
try{process.loadEnvFile(path.join(root,'.env'));}catch(e){if((e as NodeJS.ErrnoException).code!=='ENOENT')throw e;}
const text=(key:string,fallback='')=>process.env[key]??fallback;
const int=(key:string,fallback:number,min=0,max=Number.MAX_SAFE_INTEGER)=>{
  const raw=process.env[key];if(raw===undefined||raw.trim()==='')return fallback;
  const value=Number(raw);if(!Number.isInteger(value)||value<min||value>max)throw Error(`${key} 必须是 ${min} 到 ${max} 之间的整数`);return value;
};
const list=(value:string)=>value.split(',').map(x=>x.trim()).filter(Boolean);
const locks:Record<string,string>=JSON.parse(text('RESOURCE_LOCKS','{}'));
if(!locks||typeof locks!=='object'||Object.values(locks).some(x=>typeof x!=='string'))throw Error('RESOURCE_LOCKS 必须是资源名到正则字符串的 JSON 对象');
export const config = {
  FEISHU_APP_ID:text('FEISHU_APP_ID'), FEISHU_APP_SECRET:text('FEISHU_APP_SECRET'),
  CARD_UPDATE_INTERVAL_MS:int('CARD_UPDATE_INTERVAL_MS',1000,250),
  // Codex runs through its SDK with everything it keeps (login, chats, generated images) in <project>/.codex. The CODEX_HOME of the system is never read.
  CODEX_API_KEY:text('CODEX_API_KEY'), CODEX_MODEL:text('CODEX_MODEL','gpt-6-luna'), CODEX_REASONING_EFFORT:text('CODEX_REASONING_EFFORT','low'),
  TASK_TIMEOUT_MS:int('TASK_TIMEOUT_MS',0), TASK_RETENTION_DAYS:int('TASK_RETENTION_DAYS',30,1), HEALTH_PORT:int('HEALTH_PORT',8765,1,65535),
  root, database: path.resolve(root,text('DATABASE_PATH','./data/agent.sqlite')),
  data:path.join(root,'data'), user:path.join(root,'user'), codexHome:path.join(root,'.codex'),
  users:list(text('ALLOWED_USERS')), admins:list(text('ADMIN_USERS')), chats:list(text('ALLOWED_CHATS')), tenants:list(text('ALLOWED_TENANTS')),
  resources:Object.entries(locks).map(([key,pattern])=>({key,regex:new RegExp(pattern,'i')})),
};
export function restrictSecret(file:string){
  if(process.platform!=='win32'||!existsSync(file))return;
  const account=execFileSync('whoami.exe',[],{encoding:'utf8',windowsHide:true,timeout:5000}).trim();
  execFileSync('icacls.exe',[file,'/inheritance:r','/grant:r',`${account}:(F)`,'*S-1-5-18:(F)'],{windowsHide:true,timeout:5000,stdio:'pipe'});
}
export function ensureDirectories(){
  mkdirSync(config.data,{recursive:true}); mkdirSync(path.dirname(config.database),{recursive:true}); mkdirSync(config.codexHome,{recursive:true});
  // The login of Codex is a file in the project; like .env it is for this account alone. (Codex rewrites it when it refreshes the token, so this is repeated at every start.)
  restrictSecret(path.join(config.root,'.env')); restrictSecret(path.join(config.codexHome,'auth.json'));
}
