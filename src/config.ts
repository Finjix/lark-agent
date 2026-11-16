import {fileURLToPath} from 'node:url';
import path from 'node:path';
import {mkdirSync,existsSync} from 'node:fs';
import {execFileSync} from 'node:child_process';
export const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
try{process.loadEnvFile(path.join(root,'.env'));}catch(e){if((e as NodeJS.ErrnoException).code!=='ENOENT')throw e;}
const text=(key:string,fallback='')=>process.env[key]?.trim()||fallback;
const int=(key:string,fallback:number,min=0,max=Number.MAX_SAFE_INTEGER)=>{
  const raw=process.env[key];if(raw===undefined||raw.trim()==='')return fallback;
  const value=Number(raw);if(!Number.isInteger(value)||value<min||value>max)throw Error(`${key} 必须是 ${min} 到 ${max} 之间的整数`);return value;
};
const oneOf=<T extends string>(key:string,values:readonly T[],fallback:T):T=>{
  const value=text(key,fallback);if(!(values as readonly string[]).includes(value))throw Error(`${key} 只能是 ${values.join('、')} 之一`);return value as T;
};
const list=(value:string)=>value.split(',').map(x=>x.trim()).filter(Boolean);
// Left empty, like the other settings, it means none.
const locks:Record<string,string>|undefined=(()=>{try{return JSON.parse(text('RESOURCE_LOCKS')||'{}');}catch{return undefined;}})();
if(!locks||typeof locks!=='object'||Array.isArray(locks)||Object.values(locks).some(x=>typeof x!=='string'))throw Error('RESOURCE_LOCKS 必须是资源名到正则字符串的 JSON 对象');
export const THINKING_LEVELS=['off','minimal','low','medium','high','xhigh','max'] as const;
export const config = {
  FEISHU_APP_ID:text('FEISHU_APP_ID'), FEISHU_APP_SECRET:text('FEISHU_APP_SECRET'),
  CARD_UPDATE_INTERVAL_MS:int('CARD_UPDATE_INTERVAL_MS',1000,250),
  TASK_TIMEOUT_MS:int('TASK_TIMEOUT_MS',0), HEALTH_PORT:int('HEALTH_PORT',8765,1,65535),
  // Logs, workspace files and the agent's chats untouched for this long are deleted (see maintenance.ts).
  RETENTION_DAYS:int('RETENTION_DAYS',7,1),
  // The model every turn runs on. The account is the ChatGPT subscription signed in with `start.cmd login` (openai-codex).
  AGENT_PROVIDER:text('AGENT_PROVIDER','openai-codex'), AGENT_MODEL:text('AGENT_MODEL','gpt-6.1-sol'),
  AGENT_THINKING:oneOf('AGENT_THINKING',THINKING_LEVELS,'medium'),
  root, database: path.resolve(root,text('DATABASE_PATH','./data/agent.sqlite')),
  data:path.join(root,'data'), user:path.join(root,'user'),
  // pi's own state (the ChatGPT login, the chats): inside the project, never the system's ~/.pi.
  agentHome:path.join(root,'.pi'),
  // The skills every turn has (standard Agent Skills: a folder with SKILL.md) and the machine's Python their scripts run with (`PYTHON`, or
  // whatever `python` is on PATH).
  skills:path.join(root,'skills'), python:text('PYTHON','python'),
  // Commands run on a timer, outside any user's turn (see jobs.ts).
  jobs:path.join(root,'jobs.json'),
  // What skills keep for every user alike (audit-auto's review-group rules, say), in a folder of each skill's own; never cleaned up, never sent.
  shared:path.join(root,'shared'),
  users:list(text('ALLOWED_USERS')), admins:list(text('ADMIN_USERS')), chats:list(text('ALLOWED_CHATS')), tenants:list(text('ALLOWED_TENANTS')),
  resources:Object.entries(locks).map(([key,pattern])=>({key,regex:new RegExp(pattern,'i')})),
};
// pi finds its home through this, whatever the environment says, and makes no network calls of its own (catalog refreshes, version checks, telemetry).
Object.assign(process.env,{PI_CODING_AGENT_DIR:config.agentHome,PI_OFFLINE:'1',PI_SKIP_VERSION_CHECK:'1',PI_TELEMETRY:'0'});
export function restrictSecret(file:string){
  if(process.platform!=='win32'||!existsSync(file))return;
  const account=execFileSync('whoami.exe',[],{encoding:'utf8',windowsHide:true,timeout:5000}).trim();
  execFileSync('icacls.exe',[file,'/inheritance:r','/grant:r',`${account}:(F)`,'*S-1-5-18:(F)'],{windowsHide:true,timeout:5000,stdio:'pipe'});
}
export const authFile=()=>path.join(config.agentHome,'auth.json');
export function ensureDirectories(){
  for(const directory of [config.data,path.dirname(config.database),config.agentHome,config.shared])mkdirSync(directory,{recursive:true});
  // The ChatGPT login is in pi's auth.json; like .env it is for this account alone. (pi rewrites it when the token is refreshed, so this is repeated at every start.)
  restrictSecret(path.join(config.root,'.env'));restrictSecret(authFile());
}
