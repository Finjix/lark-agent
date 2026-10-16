import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {EventEmitter} from 'node:events';
import {Store} from '../dist/storage/database.js';
import {incomingMessage} from '../dist/feishu/incoming.js';
import {taskPrompt,taskRequest} from '../dist/desktop/adapter.js';
import {StreamState} from '../dist/desktop/stream.js';
import {TaskService} from '../dist/tasks/service.js';
import {Delivery} from '../dist/feishu/delivery.js';

const input=(message,instruction='  原文\n\n保留 `代码`、🙂 和空白。 \r\n')=>({tenant_key:'tenant',user_id:'user',chat_id:'chat',source_message_id:message,instruction});
const snapshot=(revision,turns)=>({type:'broadcast',method:'thread-stream-state-changed',params:{hostId:'local',conversationId:'thread',change:{type:'snapshot',revision,conversationState:{turns}}}});
const turn=(id,clientId,answer)=>({turnId:id,status:'completed',params:{clientUserMessageId:clientId,input:[{type:'text',text:'相同原文'}]},items:[{type:'agentMessage',phase:'final_answer',text:answer}]});

test('private text and rich-text whitespace reach Codex unchanged; attachment-only input is not replaced with an invented request',async()=>{
  const s=new Store(':memory:'),event={tenant_key:'tenant',sender:{sender_type:'user',sender_id:{open_id:'user'}},message:{chat_type:'p2p',chat_id:'chat',message_id:'message',message_type:'text',content:JSON.stringify({text:input('m').instruction})}};
  const d=Object.assign(new EventEmitter(),{stream:new StreamState()}),service=new TaskService(s,d);service.authorized=()=>true;
  const incoming=incomingMessage(event,'bot');await service.receive(incoming);
  assert.equal(taskPrompt(s.list()[0],s),input('m').instruction);
  const rich=incomingMessage({...event,message:{...event.message,message_type:'post',content:JSON.stringify({title:'',content:[[{tag:'text',text:'  第一行  '}],[{tag:'text',text:''}],[{tag:'text',text:'第三行\n'}]]})}},'bot');
  assert.equal(rich.text,'  第一行  \n\n第三行\n');
  s.transition(s.list()[0].id,'COMPLETED');
  await service.receive({...incoming,message:'file-only',text:'',attachments:[{kind:'file',key:'key',name:'notes.txt'}]});
  const file=s.list()[1];assert.equal(file.instruction,'');s.inputReady(file.id,0,'D:/inputs/notes.txt',5);
  const request=taskRequest({...file,thread_id:'thread',workspace_path:'D:/outputs/task'},s);
  assert(request.input[0].text.includes('## notes.txt: D:/inputs/notes.txt\n')&&!request.input[0].text.includes('My request'),'a file with no text still goes out as a file mention');assert.deepEqual(request.additionalContext,{});
  assert(!JSON.stringify(request.input).includes('deliverable'));s.close();
});

test('identical user messages are routed by persisted client ID after restart; bootstrap and previous answers never reach the next user reply',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-raw-restart-')),file=path.join(root,'agent.sqlite');let s=new Store(file);
  const old=s.create(input('old','相同原文')).task,session=s.sessionFor(old);s.bindSession(session.id,'thread',root);
  s.transition(old.id,'COMPLETED',{thread_id:'thread',turn_id:'old-turn',result:'旧回复'});
  const next=s.create(input('new','相同原文')).task;s.sessionFor(next);s.transition(next.id,'UNKNOWN',{thread_id:'thread',workspace_path:root});
  s.event(next.id,'DESKTOP_RAW_REQUEST',{});s.event(next.id,'DESKTOP_TURN_SUBMITTING',{clientUserMessageId:next.id});s.close();s=new Store(file);
  const d=Object.assign(new EventEmitter(),{stream:new StreamState(),follow:async()=>{},unfollow:()=>{}}),service=new TaskService(s,d);
  d.stream.receive(snapshot(1,[turn('bootstrap','init','READY'),turn('old-turn',old.id,'旧回复')]));service.onState('thread');
  assert.equal(s.get(next.id).status,'UNKNOWN');assert.equal(s.get(next.id).result,'');
  d.stream.receive(snapshot(2,[turn('bootstrap','init','READY'),turn('old-turn',old.id,'旧回复'),turn('new-turn',next.id,'正确新回复')]));service.onState('thread');
  assert.equal(s.get(next.id).status,'COMPLETED');assert.equal(s.get(next.id).turn_id,'new-turn');assert.equal(s.get(next.id).result,'正确新回复');
  assert.equal(s.get(old.id).result,'旧回复');await new Promise(r=>setImmediate(r));s.close();await fs.rm(root,{recursive:true});
});

test('raw-turn replies still collect and deliver linked files; repeated delivery does not resend the file',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-raw-delivery-')),s=new Store(':memory:'),task=s.create(input('file')).task;
  const report=path.join(root,'交付.txt');await fs.writeFile(report,'原文交付校验');
  s.transition(task.id,'STARTING',{thread_id:'thread',workspace_path:root});s.event(task.id,'DESKTOP_TURN_SUBMITTING',{clientUserMessageId:task.id});
  const d=Object.assign(new EventEmitter(),{stream:new StreamState(),unfollow:()=>{}}),service=new TaskService(s,d);
  const answer=`已完成，[下载文件](<${report}>)`;
  d.stream.receive(snapshot(1,[turn('raw-turn',task.id,answer)]));service.onState('thread');
  const sent=[],uploads=[],api={connected:true,raw:async(_method,url)=>url.endsWith('/cards')?{card_id:'card'}:{},reply:async(...args)=>{sent.push(args);return {message_id:'sent'};},upload:async file=>{uploads.push(await fs.readFile(file,'utf8'));return {file_key:'uploaded'};}};
  const delivery=new Delivery(s,api);await delivery.tick();await delivery.tick();await delivery.tick();
  assert.deepEqual(uploads,['原文交付校验']);assert.equal(sent.filter(a=>a[2]==='file').length,1);assert.equal(s.get(task.id).result,answer);
  await delivery.stop();await new Promise(r=>setImmediate(r));s.close();await fs.rm(root,{recursive:true});
});

test('reconciliation voids an input that never went out and never replays an uncertain business send',async()=>{
  const s=new Store(':memory:'),d=Object.assign(new EventEmitter(),{stream:new StreamState(),follow:async()=>{}}),service=new TaskService(s,d);
  for(const message of ['initializing','sent']){
    const t=s.create({...input(message),user_id:message}).task,session=s.sessionFor(t);s.bindSession(session.id,`thread-${message}`,'workspace');
    s.transition(t.id,'UNKNOWN',{thread_id:`thread-${message}`});s.event(t.id,'DESKTOP_RAW_REQUEST',{});
    if(message==='sent')s.event(t.id,'DESKTOP_TURN_SUBMITTING',{clientUserMessageId:t.id});
  }
  await service.reconcile();assert.deepEqual(s.list().map(t=>t.status),['FAILED','UNKNOWN']);assert.equal(s.list()[0].error,'机器人罢工了，请联系 @钟丰骏-实习生');s.close();
});

test('a raw turn still returns an input request and routes the user answer to its exact thread, turn and numeric request ID',async()=>{
  const s=new Store(':memory:'),task=s.create(input('question','向我提问')).task,calls=[];
  const d=Object.assign(new EventEmitter(),{stream:new StreamState(),respond:async(...args)=>calls.push(args)}),service=new TaskService(s,d);service.authorized=()=>true;
  s.transition(task.id,'STARTING',{thread_id:'thread'});s.event(task.id,'DESKTOP_TURN_SUBMITTING',{clientUserMessageId:task.id});
  const waiting=turn('question-turn',task.id,'');waiting.status='inProgress';
  const update=snapshot(1,[waiting]);update.params.change.conversationState.requests=[{id:9,method:'item/tool/requestUserInput',params:{turnId:'question-turn',questions:[{id:'choice',question:'请选择',options:[{label:'a'},{label:'b'},{label:'c'}]}]}}];
  d.stream.receive(update);service.onState('thread');assert.equal(s.get(task.id).status,'WAITING_APPROVAL');assert.equal(s.get(task.id).turn_id,'question-turn');
  assert.equal(s.pending(task.id)[0].id,'9');
  await service.receive({tenant:'tenant',user:'user',chat:'chat',message:'answer',isGroup:false,text:`/answer ${task.id} 9 a`});
  assert.equal(calls.length,1);assert.deepEqual(calls[0].slice(0,2),['thread','question-turn']);assert.equal(calls[0][2].payload._requestId,9);assert.equal(calls[0][3],'a');
  assert.deepEqual(s.pending(task.id),[]);s.close();
});
