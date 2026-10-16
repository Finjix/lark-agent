import fs from 'node:fs';
import path from 'node:path';
import {config} from './config.js';

/**
 * The standing instructions for Feishu chats live in prompt/system.md and are written into Codex's global instructions file
 * (the one Desktop shows under Personalization, ~/.codex/AGENTS.md) as one marked block. The text itself says it only applies
 * in lark-<id> folders, so the user's own chats are unaffected.
 */
const BEGIN='<!-- >>> lark-agent (managed, do not edit) >>> -->',END='<!-- <<< lark-agent (managed) <<< -->';
export const promptSource=()=>path.join(config.root,'prompt','system.md');
const target=()=>path.join(config.codexHome,'AGENTS.md');
const normalize=(text:string)=>text.split('\r\n').join('\n');
const escape=(text:string)=>text.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
const managed=()=>new RegExp(`\\n?${escape(BEGIN)}[\\s\\S]*?${escape(END)}\\n?`);
const block=()=>`${BEGIN}\n${normalize(fs.readFileSync(promptSource(),'utf8')).trim()}\n${END}`;
const read=()=>{try{return fs.readFileSync(target(),'utf8');}catch(e){if((e as NodeJS.ErrnoException).code==='ENOENT')return '';throw e;}};
const write=(content:string)=>{
  const file=target(),temp=`${file}.lark-agent.tmp`;
  if(fs.existsSync(file)&&!fs.existsSync(`${file}.lark-agent.bak`))fs.copyFileSync(file,`${file}.lark-agent.bak`);
  fs.mkdirSync(path.dirname(file),{recursive:true});fs.writeFileSync(temp,content);fs.renameSync(temp,file);
};

export type PromptResult='registered'|'updated'|'unchanged'|'removed'|'absent';
export function setPrompt():PromptResult{
  const current=normalize(read()),wanted=block(),pattern=managed();
  if(pattern.test(current)){
    if(current.match(pattern)![0].trim()===wanted)return 'unchanged';
    write(current.replace(pattern,`\n${wanted}\n`));return 'updated';
  }
  write(`${current}${current&&!current.endsWith('\n')?'\n':''}${current.trim()?'\n':''}${wanted}\n`);return 'registered';
}
export function removePrompt():PromptResult{
  const current=normalize(read());if(!managed().test(current))return 'absent';
  const rest=current.replace(managed(),'');write(rest.trim()?`${rest.trimEnd()}\n`:'');return 'removed';
}
export const promptCurrent=()=>{try{return normalize(read()).includes(block());}catch{return false;}};
