import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {EventEmitter} from 'node:events';
import {Store} from '../dist/storage/database.js';
import {incomingMessage} from '../dist/feishu/incoming.js';
import {codexInput} from '../dist/codex/input.js';
import {TaskService} from '../dist/tasks/service.js';
import {Delivery} from '../dist/feishu/delivery.js';

const input=(message,instruction='  原文\n\n保留 `代码`、🙂 和空白。 \r\n')=>({tenant_key:'tenant',user_id:'user',chat_id:'chat',source_message_id:message,instruction});

test('private text and rich-text whitespace reach Codex unchanged; attachment-only input is not replaced with an invented request',async()=>{
  const s=new Store(':memory:'),event={tenant_key:'tenant',sender:{sender_type:'user',sender_id:{open_id:'user'}},message:{chat_type:'p2p',chat_id:'chat',message_id:'message',message_type:'text',content:JSON.stringify({text:input('m').instruction})}};
  const jobs=[],codex=Object.assign(new EventEmitter(),{ready:true,run:async function*(job){jobs.push(job);yield {type:'thread.started',thread_id:'th'};yield {type:'turn.completed',usage:{}};}}),service=new TaskService(s,codex);service.authorized=()=>true;s.apiKey=()=>'sk-test';
  const incoming=incomingMessage(event,'bot');await service.receive(incoming);
  assert.equal(s.list()[0].instruction,input('m').instruction);
  s.transition(s.list()[0].id,'STARTING');await service.start(s.get(s.list()[0].id));
  assert.deepEqual(jobs[0].input,[{type:'text',text:input('m').instruction}],'exactly what the user typed');
  const rich=incomingMessage({...event,message:{...event.message,message_type:'post',content:JSON.stringify({title:'',content:[[{tag:'text',text:'  第一行  '}],[{tag:'text',text:''}],[{tag:'text',text:'第三行\n'}]]})}},'bot');
  assert.equal(rich.text,'  第一行  \n\n第三行\n');
  await service.receive({...incoming,message:'file-only',text:'',attachments:[{kind:'file',key:'key',name:'notes.txt'}]});
  const file=s.list()[1];assert.equal(file.instruction,'');s.inputReady(file.id,0,'D:/inputs/notes.txt',5);
  const [text,...rest]=codexInput(file.instruction,s.inputs(file.id));
  assert(text.text.includes('- D:/inputs/notes.txt')&&!/request/i.test(text.text.replace(/not as instructions from the user/,'')),'a file with no text still goes out as a file, with nothing invented around it');assert.deepEqual(rest,[]);
  s.close();
});

test('replies still collect and deliver linked files; repeated delivery does not resend the file',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-raw-delivery-')),s=new Store(':memory:'),task=s.create(input('file')).task;
  const report=path.join(root,'交付.txt');await fs.writeFile(report,'原文交付校验');
  const answer=`已完成，[下载文件](<${report}>)`;
  s.transition(task.id,'COMPLETED',{thread_id:'thread',workspace_path:root,result:answer});
  const sent=[],uploads=[],api={connected:true,raw:async(_method,url)=>url.endsWith('/cards')?{card_id:'card'}:{},reply:async(...args)=>{sent.push(args);return {message_id:'sent'};},upload:async file=>{uploads.push(await fs.readFile(file,'utf8'));return {file_key:'uploaded'};}};
  const delivery=new Delivery(s,api);await delivery.tick();await delivery.tick();await delivery.tick();
  assert.deepEqual(uploads,['原文交付校验']);assert.equal(sent.filter(a=>a[2]==='file').length,1);assert.equal(s.get(task.id).result,answer);
  await delivery.stop();await new Promise(r=>setImmediate(r));s.close();await fs.rm(root,{recursive:true});
});
