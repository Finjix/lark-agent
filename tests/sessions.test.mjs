import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {EventEmitter} from 'node:events';
import {Store} from '../dist/storage/database.js';
import {NativeDesktop,taskPrompt,taskWorkspace,SESSION_INIT_PROMPT} from '../dist/desktop/adapter.js';
import {TaskService} from '../dist/tasks/service.js';
import {StreamState} from '../dist/desktop/stream.js';
import {cleanup} from '../dist/maintenance.js';
import {OutcomeUnknown} from '../dist/desktop/transport.js';

const input=(message='one',user='user',tenant='tenant')=>({tenant_key:tenant,user_id:user,chat_id:'chat',source_message_id:message,instruction:message});

test('session ownership survives restart and is isolated by tenant and user',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-session-db-')),file=path.join(root,'db.sqlite');let s=new Store(file);
  const a=s.create(input()).task,b=s.create(input('two')).task,c=s.create(input('other-one','other')).task,d=s.create(input('one','user','other-tenant')).task;
  const session=s.sessionFor(a);s.bindSession(session.id,'thread','workspace');
  assert.equal(s.sessionFor(b).id,session.id);assert.notEqual(s.sessionFor(c).id,session.id);assert.notEqual(s.sessionFor(d).id,session.id);
  assert.throws(()=>s.bindSession(session.id,'wrong-thread'),/其他/);
  s.close();s=new Store(file);assert.equal(s.sessionFor(s.get(b.id)).thread_id,'thread');assert.equal(s.get(b.id).session_id,session.id);
  s.close();await fs.rm(root,{recursive:true});
});

test('Desktop creates one user thread and sends subsequent messages to that same thread with separate output directories',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-session-native-')),s=new Store(':memory:'),desktop=new NativeDesktop(s),calls=[];
  desktop.ready=true;desktop.follow=async()=>{};desktop.waitForIdle=async()=>{};
  desktop.tools={call:async(name,args)=>{calls.push({name,args});return {threadId:'same-thread',projectlessOutputDirectory:root};}};
  desktop.ipc={request:async(name,args)=>{calls.push({name,args});return {result:{result:{turn:{id:`turn-${calls.length}`}}}};}};
  const a=s.create(input()).task;s.sessionFor(a);const first=await desktop.startTask(s.get(a.id));
  const b=s.create(input('two')).task;s.sessionFor(b);const second=await desktop.startTask(s.get(b.id));
  assert.equal(first.threadId,second.threadId);assert.notEqual(first.workspace,second.workspace);
  assert.deepEqual(calls.map(c=>c.name),['create_thread','thread-follower-start-turn','thread-follower-start-turn']);
  assert.equal(calls[0].args.prompt,SESSION_INIT_PROMPT);
  assert.equal(calls[1].args.turnStart.request.input[0].text,'one');
  const request=calls[2].args.turnStart.request;
  assert.equal(request.threadId,'same-thread');assert.equal(request.input[0].text,'two');
  assert.equal(request.clientUserMessageId,b.id);assert(request.additionalContext.lark_delivery_instructions.value.includes(second.workspace));
  assert.equal(s.get(b.id).thread_id,'same-thread');assert.equal(s.get(b.id).workspace_path,second.workspace);
  s.close();await fs.rm(root,{recursive:true});
});

test('stream updates route to the active message and never reuse the previous turn reply',async()=>{
  const s=new Store(':memory:'),desktop=Object.assign(new EventEmitter(),{stream:new StreamState(),unfollow:()=>{}}),service=new TaskService(s,desktop);
  const old=s.create(input()).task,session=s.sessionFor(old);s.bindSession(session.id,'thread','workspace');
  s.transition(old.id,'COMPLETED',{thread_id:'thread',turn_id:'old',result:'previous answer'});
  const next=s.create(input('new message')).task;s.sessionFor(next);s.transition(next.id,'STARTING',{thread_id:'thread'});
  const turn=(id,task,answer)=>({turnId:id,status:'completed',params:{input:[{type:'text',text:`[LARK_TASK_ID:${task.id}]`}]},items:[{type:'agentMessage',phase:'final_answer',text:answer}]});
  desktop.stream.receive({type:'broadcast',method:'thread-stream-state-changed',params:{hostId:'local',conversationId:'thread',change:{type:'snapshot',revision:1,conversationState:{turns:[turn('old',old,'previous answer')]}}}});
  service.onState('thread');assert.equal(s.get(next.id).status,'STARTING');assert.equal(s.get(next.id).result,'');
  desktop.stream.receive({type:'broadcast',method:'thread-stream-state-changed',params:{hostId:'local',conversationId:'thread',change:{type:'snapshot',revision:2,conversationState:{turns:[turn('old',old,'previous answer'),turn('new',next,'new answer')]}}}});
  service.onState('thread');assert.equal(s.get(next.id).status,'COMPLETED');assert.equal(s.get(next.id).turn_id,'new');assert.equal(s.get(next.id).result,'new answer');assert.equal(s.get(old.id).result,'previous answer');
  await new Promise(resolve=>setImmediate(resolve));s.close();
});

test('an uncertain continuation retains the known thread and never sends the message twice during reconciliation',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-session-unknown-')),s=new Store(':memory:'),d=new NativeDesktop(s);let sent=0;
  const first=s.create(input()).task,session=s.sessionFor(first);s.bindSession(session.id,'thread',root);s.transition(first.id,'COMPLETED');
  const next=s.create(input('next')).task;d.ready=true;d.follow=async()=>{};d.waitForIdle=async()=>{};d.tools={};
  d.ipc={request:async()=>{sent++;throw new OutcomeUnknown('lost response');}};
  const service=new TaskService(s,d);s.transition(next.id,'STARTING');await service.start(s.get(next.id));
  assert.equal(s.get(next.id).status,'UNKNOWN');assert.equal(s.get(next.id).thread_id,'thread');await service.reconcile();assert.equal(sent,1);
  s.close();await fs.rm(root,{recursive:true});
});

test('expiring a completed message removes only its output directory and keeps the user session and later files',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-session-retain-')),s=new Store(':memory:');
  const old=s.create(input()).task,session=s.sessionFor(old);s.bindSession(session.id,'thread',root);
  const oldDir=taskWorkspace(root,old.id);await fs.mkdir(oldDir,{recursive:true});await fs.writeFile(path.join(oldDir,'old.txt'),'old');
  s.transition(old.id,'COMPLETED',{thread_id:'thread',workspace_path:oldDir});s.update(old.id,{finished_at:1});
  const row=s.due()[0];s.ack(row.id,row.version);
  const later=s.create(input('later')).task;s.sessionFor(later);const laterDir=taskWorkspace(root,later.id);await fs.mkdir(laterDir,{recursive:true});await fs.writeFile(path.join(laterDir,'later.txt'),'keep');
  let archived=false;await cleanup(s,{archive:async()=>{archived=true;}});
  assert.equal(archived,false);assert(s.get(old.id).cleaned_at);assert.equal(await fs.readFile(path.join(laterDir,'later.txt'),'utf8'),'keep');assert.equal(s.session(session.id).thread_id,'thread');
  await assert.rejects(fs.stat(oldDir),{code:'ENOENT'});assert.equal(taskPrompt(s.get(later.id),s),'later');s.close();await fs.rm(root,{recursive:true});
});
