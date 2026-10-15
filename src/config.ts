import {fileURLToPath} from 'node:url';
import path from 'node:path';
import {mkdirSync,existsSync} from 'node:fs';
import {execFileSync} from 'node:child_process';
import os from 'node:os';
import dotenv from 'dotenv';
import {z} from 'zod';
export const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
dotenv.config({path: path.join(root,'.env'), quiet: true});
const number = (value: number, min = 0) => z.coerce.number().int().min(min).default(value);
const schema = z.object({
  FEISHU_APP_ID: z.string().default(''), FEISHU_APP_SECRET: z.string().default(''),
  ALLOWED_USERS: z.string().default(''), ADMIN_USERS: z.string().default(''), ALLOWED_CHATS: z.string().default(''), ALLOWED_TENANTS:z.string().default(''),
  DATABASE_PATH:z.string().default('./data/agent.sqlite'),
  CARD_UPDATE_INTERVAL_MS:number(1000,250), DESKTOP_CONNECT_TIMEOUT_MS:number(10000,500),
  TASK_TIMEOUT_MS:number(0), TASK_RETENTION_DAYS:number(30,1), HEALTH_PORT:z.coerce.number().int().min(1).max(65535).default(8765),
  RESOURCE_LOCKS:z.string().default('{}'), DESKTOP_SUPPORTED_VERSIONS:z.string().default('26.1007.9446.0'),
});
const raw = schema.parse(process.env);
const list=(value:string)=>value.split(',').map(x=>x.trim()).filter(Boolean);
const locks = z.record(z.string(),z.string()).parse(JSON.parse(raw.RESOURCE_LOCKS));
export const config = {...raw, root, database: path.resolve(root,raw.DATABASE_PATH),
  data:path.join(root,'data'), codexHome:process.env.CODEX_HOME || path.join(os.homedir(),'.codex'),
  users:list(raw.ALLOWED_USERS), admins:list(raw.ADMIN_USERS), chats:list(raw.ALLOWED_CHATS), tenants:list(raw.ALLOWED_TENANTS),
  versions:list(raw.DESKTOP_SUPPORTED_VERSIONS), resources:Object.entries(locks).map(([key,pattern])=>({key,regex:new RegExp(pattern,'i')})),
};
export function restrictSecret(file:string){
  if(process.platform!=='win32'||!existsSync(file))return;
  const account=execFileSync('whoami.exe',[],{encoding:'utf8',windowsHide:true,timeout:5000}).trim();
  execFileSync('icacls.exe',[file,'/inheritance:r','/grant:r',`${account}:(F)`,'*S-1-5-18:(F)'],{windowsHide:true,timeout:5000,stdio:'pipe'});
}
export function ensureDirectories(){ mkdirSync(config.data,{recursive:true}); mkdirSync(path.dirname(config.database),{recursive:true});restrictSecret(path.join(config.root,'.env')); }
