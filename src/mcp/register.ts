import fs from 'node:fs';
import path from 'node:path';
import {config} from '../config.js';
import {mcpUrl} from './server.js';

const BEGIN='# >>> lark-agent (managed, do not edit) >>>',END='# <<< lark-agent (managed) <<<';
const SERVER='lark_agent';
const configFile=()=>path.join(config.codexHome,'config.toml');
const block=(token:string)=>`${BEGIN}
[mcp_servers.${SERVER}]
url = "${mcpUrl()}"
http_headers = { Authorization = "Bearer ${token}" }
default_tools_approval_mode = "approve"
tool_timeout_sec = 150
${END}`;
const managed=new RegExp(`\\r?\\n?${BEGIN.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')}[\\s\\S]*?${END.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')}\\r?\\n?`);
const normalize=(text:string)=>text.split('\r\n').join('\n');
const read=()=>{try{return fs.readFileSync(configFile(),'utf8');}catch(e){if((e as NodeJS.ErrnoException).code==='ENOENT')return '';throw e;}};
const write=(content:string)=>{
  const file=configFile(),temp=`${file}.lark-agent.tmp`;
  if(fs.existsSync(file)&&!fs.existsSync(`${file}.lark-agent.bak`))fs.copyFileSync(file,`${file}.lark-agent.bak`);
  fs.mkdirSync(path.dirname(file),{recursive:true});fs.writeFileSync(temp,content);fs.renameSync(temp,file);
};

export type Registration='registered'|'updated'|'unchanged'|'removed'|'absent';
/** Adds (or refreshes) the lark_agent MCP server in Codex's config.toml inside a clearly marked block. Never touches anything else. */
export function registerMcp(token:string):Registration{
  const current=read();
  if(!managed.test(current)&&new RegExp(`^\\[mcp_servers\\.${SERVER}[\\].]`,'m').test(current))throw Error(`${configFile()} 里已有手写的 [mcp_servers.${SERVER}]，请先删除或改名`);
  if(managed.test(current)){
    const existing=normalize(current.match(managed)![0]).trim();
    if(existing===block(token))return 'unchanged';
    write(current.replace(managed,`\n${block(token)}\n`));return 'updated';
  }
  write(`${current}${current&&!current.endsWith('\n')?'\n':''}${current?'\n':''}${block(token)}\n`);return 'registered';
}
export function unregisterMcp():Registration{
  const current=read();if(!managed.test(current))return 'absent';
  write(current.replace(managed,''));return 'removed';
}
export function mcpRegistered(token:string){return normalize(read()).includes(block(token));}
