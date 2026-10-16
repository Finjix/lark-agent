import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {config} from '../dist/config.js';
import {setPrompt,removePrompt,promptCurrent,promptSource} from '../dist/prompt.js';

async function sandbox(t){
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-prompt-')),saved={root:config.root,home:config.codexHome};
  config.root=path.join(root,'project');config.codexHome=path.join(root,'codex');
  await fs.mkdir(path.join(config.root,'prompt'),{recursive:true});await fs.mkdir(config.codexHome,{recursive:true});await fs.writeFile(promptSource(),'Rule one.\n\nRule two.\n');
  t.after(async()=>{Object.assign(config,{root:saved.root,codexHome:saved.home});await fs.rm(root,{recursive:true,force:true});});
  return path.join(config.codexHome,'AGENTS.md');
}

test('the shipped system prompt is short, static, names the output folder, and only applies in lark- folders',async()=>{
  const text=await fs.readFile(path.join(config.root,'prompt','system.md'),'utf8');
  assert(/lark-/.test(text)&&/output/.test(text)&&!/lark_user_attachments/.test(text));
  assert(text.length<300,'every character here is paid for in every chat');
  assert(!/<task|\$\{/.test(text),'it is static: nothing in it changes from one task to the next');
});

test('set-prompt adds one marked block, keeps the user\'s own instructions, and is idempotent',async t=>{
  const file=await sandbox(t);
  assert.equal(promptCurrent(),false);
  assert.equal(setPrompt(),'registered');assert(promptCurrent());
  let text=await fs.readFile(file,'utf8');assert(text.includes('Rule one.')&&text.includes('Rule two.'));assert.equal(text.split('lark-agent (managed, do not edit)').length,2);
  assert.equal(setPrompt(),'unchanged');

  await fs.writeFile(file,'My own instructions.\n');
  assert.equal(setPrompt(),'registered');
  text=await fs.readFile(file,'utf8');assert(text.startsWith('My own instructions.\n'));assert(text.includes('Rule one.'));
  assert.equal(await fs.readFile(`${file}.lark-agent.bak`,'utf8'),'My own instructions.\n','the original is backed up once');
  await fs.writeFile(file,text.split('\n').join('\r\n'));assert.equal(setPrompt(),'unchanged','CRLF line endings do not cause rewrites');
});

test('editing prompt/system.md and running set-prompt again updates the block in place',async t=>{
  const file=await sandbox(t);await fs.writeFile(file,'Mine.\n');setPrompt();
  await fs.writeFile(promptSource(),'Only rule now.\n');assert.equal(promptCurrent(),false);
  assert.equal(setPrompt(),'updated');
  const text=await fs.readFile(file,'utf8');assert(text.includes('Only rule now.')&&!text.includes('Rule one.'));assert.equal(text.split('lark-agent (managed, do not edit)').length,2);assert(text.startsWith('Mine.'));
});

test('set-prompt remove takes out only its own block',async t=>{
  const file=await sandbox(t);await fs.writeFile(file,'Mine.\n');setPrompt();
  assert.equal(removePrompt(),'removed');assert.equal((await fs.readFile(file,'utf8')).trim(),'Mine.');assert.equal(removePrompt(),'absent');
  await fs.writeFile(file,'');setPrompt();assert.equal(removePrompt(),'removed');assert.equal(await fs.readFile(file,'utf8'),'','a file that held nothing else is left empty');
});
