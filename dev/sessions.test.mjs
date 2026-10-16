import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {EventEmitter} from 'node:events';
import {config} from '../dist/config.js';
import {Store} from '../dist/storage/database.js';
import {NativeDesktop,taskPrompt,taskWorkspace,SESSION_INIT_PROMPT} from '../dist/desktop/adapter.js';
import {RpcRejected} from '../dist/desktop/transport.js';
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

test('Desktop creates one user thread and sends subsequent messages to that same thread with a shared output folder',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-session-native-')),s=new Store(':memory:'),desktop=new NativeDesktop(s,()=> 'active'),calls=[];
  desktop.ready=true;desktop.follow=async()=>{};desktop.waitForIdle=async()=>{};
  desktop.tools={call:async(name,args)=>{calls.push({name,args});return {threadId:'same-thread',thread:{cwd:root}};}};
  desktop.ipc={request:async(name,args)=>{calls.push({name,args});return {result:{result:{turn:{id:`turn-${calls.length}`}}}};}};
  const a=s.create(input()).task;s.sessionFor(a);const first=await desktop.startTask(s.get(a.id));
  const b=s.create(input('two')).task;s.sessionFor(b);const second=await desktop.startTask(s.get(b.id));
  assert.equal(first.threadId,second.threadId);assert.equal(first.workspace,second.workspace,'one plain output folder per chat');
  assert.equal(first.workspace,path.join(root,'output'),"outputs live in Codex's own chat folder, not under user/");
  await assert.rejects(fs.stat(first.workspace),{code:'ENOENT'},'the output folder is not created until something is written');
  const real=calls.filter(c=>c.name!=='read_thread');
  assert.deepEqual(real.map(c=>c.name),['create_thread','thread-follower-start-turn','thread-follower-start-turn']);
  assert.equal(real[0].args.prompt,SESSION_INIT_PROMPT);
  assert.equal(real[1].args.turnStart.request.input[0].text,'one');
  const request=real[2].args.turnStart.request;
  assert.equal(request.threadId,'same-thread');assert.equal(request.input[0].text,'two');
  assert.equal(request.clientUserMessageId,b.id);assert.deepEqual(request.additionalContext,{},'nothing is injected into a turn without attachments');
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
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-session-unknown-')),s=new Store(':memory:'),d=new NativeDesktop(s,()=> 'active');let sent=0;
  const first=s.create(input()).task,session=s.sessionFor(first);s.bindSession(session.id,'thread',root);s.transition(first.id,'COMPLETED');
  const next=s.create(input('next')).task;d.ready=true;d.follow=async()=>{};d.waitForIdle=async()=>{};d.tools={};
  d.ipc={request:async()=>{sent++;throw new OutcomeUnknown('lost response');}};
  const service=new TaskService(s,d);s.transition(next.id,'STARTING');await service.start(s.get(next.id));
  assert.equal(s.get(next.id).status,'UNKNOWN');assert.equal(s.get(next.id).thread_id,'thread');await service.reconcile();assert.equal(sent,1);
  s.close();await fs.rm(root,{recursive:true});
});

test('expiring a completed message removes only the files it delivered and keeps the output folder, the session and later files',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-session-retain-')),s=new Store(':memory:');
  const old=s.create(input()).task,session=s.sessionFor(old);s.bindSession(session.id,'thread',root);
  const output=taskWorkspace(root);await fs.mkdir(output,{recursive:true});
  const oldFile=path.join(output,'old.txt'),laterFile=path.join(output,'later.txt');await fs.writeFile(oldFile,'old');await fs.writeFile(laterFile,'keep');
  s.transition(old.id,'COMPLETED',{thread_id:'thread',workspace_path:output});s.update(old.id,{finished_at:1});
  s.db.prepare('INSERT INTO task_artifacts(id,task_id,path,name,size,kind) VALUES(?,?,?,?,?,?)').run('a1',old.id,oldFile,'old.txt',3,'file');
  const row=s.due()[0];s.ack(row.id,row.version);
  const later=s.create(input('later')).task;s.sessionFor(later);
  let archived=false;await cleanup(s,{archive:async()=>{archived=true;}});
  assert.equal(archived,false);assert(s.get(old.id).cleaned_at);assert.equal(s.session(session.id).thread_id,'thread');
  await assert.rejects(fs.stat(oldFile),{code:'ENOENT'});assert.equal(await fs.readFile(laterFile,'utf8'),'keep');assert((await fs.stat(output)).isDirectory());
  assert.equal(taskPrompt(s.get(later.id),s),'later');s.close();await fs.rm(root,{recursive:true});
});

test("the chat folder is whatever Desktop says it is, and a stored folder follows it",async()=>{
  const s=new Store(':memory:'),desktop=new NativeDesktop(s,()=>'active'),moved=path.join(os.tmpdir(),'lark-moved-chat');let cwd=path.join(os.tmpdir(),'lark-first-chat');
  desktop.ready=true;desktop.tools={call:async(name)=>name==='read_thread'?{thread:{cwd}}:{threadId:'chat-thread'}};
  const a=s.create(input('f1','folder-user')).task;
  const session=await desktop.prepareSession(s.get(a.id));
  assert.equal(session.thread_id,'chat-thread');assert.equal(session.workspace_path,path.resolve(cwd));
  cwd=moved;const again=await desktop.prepareSession(s.get(a.id));
  assert.equal(again.workspace_path,path.resolve(moved),'a later look at the thread updates the stored folder');
  desktop.tools={call:async()=>({thread:{}})};
  assert.equal((await desktop.prepareSession(s.get(a.id))).workspace_path,path.resolve(moved),'if Desktop cannot be asked, the known folder is kept');
  // A brand-new chat whose folder cannot be read is not used, but it stays bound so a retry does not create a second chat.
  desktop.tools={call:async(name)=>name==='read_thread'?{thread:{}}:{threadId:'new-thread'}};
  const fresh=s.create(input('f2','other-user')).task;
  await assert.rejects(desktop.prepareSession(s.get(fresh.id)),/工作目录/);
  const bound=s.sessionFor(s.get(fresh.id));assert.equal(bound.thread_id,'new-thread');assert.equal(bound.workspace_path,null);
  s.close();
});
