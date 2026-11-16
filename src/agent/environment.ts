import path from 'node:path';
import {config} from '../config.js';

/**
 * Whose turn it is, as far as skills are concerned: `dir` is user/<name> (not its workspace), `openId` the Feishu Open ID, `chat` the chat
 * (private or group) the turn's message came from and `chatType` its kind ('p2p' or 'group', known only from a message received since the
 * service started). Every message of a turn comes from that chat: one sent from another chat is not steered in.
 */
export interface SkillUser {dir:string;openId:string;chat?:string;chatType?:string}
/** What a skill's scripts get from the host, besides the service's own environment. */
export const SKILL_ENV=['LARK_AGENT_USER_DIR','LARK_AGENT_USER_OPEN_ID','LARK_AGENT_CHAT_ID','LARK_AGENT_CHAT_TYPE','LARK_AGENT_SHARED_DIR','FEISHU_APP_ID','FEISHU_APP_SECRET'] as const;
/** The project's Python environment: what `python` means to a skill. */
export const venvScripts=()=>path.join(config.venv,process.platform==='win32'?'Scripts':'bin');
export const venvPython=()=>path.join(venvScripts(),process.platform==='win32'?'python.exe':'python');

/** The environment with these folders in front of PATH (Windows spells the variable any way; one spelling is kept). */
export function withPath(env:Record<string,string>,folders:string[],platform=process.platform):Record<string,string>{
  if(!folders.length)return env;
  const result={...env},keys=Object.keys(result).filter(k=>k.toLowerCase()==='path');
  const key=platform!=='win32'?'PATH':keys.includes('Path')?'Path':keys.at(-1)??'PATH';
  if(platform==='win32')for(const other of keys)if(other!==key)delete result[other];
  result[key]=[...folders,...(result[key]??'').split(path.delimiter).filter(entry=>entry&&!folders.includes(entry))].join(path.delimiter);
  return result;
}

/**
 * The environment of every command the agent runs and of every job: the service's own (so `.env` settings such as AUDIT_AUTO_GEMINI_KEY reach
 * the skills), with the project's Python first on PATH and reading and writing UTF-8, the folder skills share and the bot's Feishu app. For a
 * user's turn it also says whose turn it is (see SkillUser). What describes a user is never inherited from the service, and a value that is not
 * set is left out rather than passed empty.
 */
export function skillEnv(user?:SkillUser,base:NodeJS.ProcessEnv=process.env):Record<string,string>{
  const env:Record<string,string>={};
  for(const [key,value] of Object.entries(base))if(value!==undefined&&!(SKILL_ENV as readonly string[]).includes(key.toUpperCase()))env[key]=value;
  const given:Record<string,string|undefined>={LARK_AGENT_SHARED_DIR:config.shared,FEISHU_APP_ID:config.FEISHU_APP_ID,FEISHU_APP_SECRET:config.FEISHU_APP_SECRET,
    ...(user?{LARK_AGENT_USER_DIR:user.dir,LARK_AGENT_USER_OPEN_ID:user.openId,LARK_AGENT_CHAT_ID:user.chat,LARK_AGENT_CHAT_TYPE:user.chatType}:{})};
  for(const [key,value] of Object.entries(given))if(value)env[key]=value;
  return withPath({...env,VIRTUAL_ENV:config.venv,PYTHONUTF8:'1',PYTHONIOENCODING:'utf-8'},[venvScripts()]);
}
