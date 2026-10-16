import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {EventEmitter} from 'node:events';
import {Database} from '../dist/storage/sqlite.js';
import {Store} from '../dist/storage/database.js';
import {NativeDesktop} from '../dist/desktop/adapter.js';
import {TaskService} from '../dist/tasks/service.js';
import {FeishuClient} from '../dist/feishu/client.js';
import {sessionTitle} from '../dist/sessions/naming.js';

const input=(message='one',user='user')=>({tenant_key:'tenant',user_id:user,chat_id:'chat',source_message_id:message,instruction:'用户原文'});

test('session names use Beijing creation time and seconds plus hundredths, including midnight and leading zeroes',()=>{
  assert.equal(sessionTitle('薯薯',Date.parse('2026-10-15T11:58:12.349Z')),'薯薯_2026_1015_1958_1234');
  assert.equal(sessionTitle(' 薯薯\n ',Date.parse('2026-10-15T16:00:00.009Z')),'薯薯_2026_1016_0000_0000');
});

test('existing sessions gain naming metadata without changing owner, thread, directory or creation time; failed title updates retry after restart',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-name-db-')),file=path.join(root,'agent.sqlite');let store=new Store(file);
  const task=store.create(input()).task,session=store.sessionFor(task);store.bindSession(session.id,'existing-thread',root);store.close();
  const legacy=new Database(file);for(const column of ['display_name','title','applied_title'])legacy.exec(`ALTER TABLE user_sessions DROP COLUMN ${column}`);legacy.close();
  store=new Store(file);const named=store.nameSession(session.id,'薯薯');assert.equal(named.created_at,session.created_at);assert.equal(named.thread_id,'existing-thread');assert.equal(named.workspace_path,root);assert.equal(named.applied_title,null);
  const d=new NativeDesktop(store,()=> 'active'),calls=[];d.ready=true;d.tools={call:async(tool,args)=>{calls.push({tool,args});throw Error('temporary failure');}};
  await d.syncSessionTitle(session.id);assert.equal(store.session(session.id).applied_title,null);store.close();
  store=new Store(file);const resumed=new NativeDesktop(store,()=> 'active');resumed.ready=true;resumed.tools={call:async(tool,args)=>{calls.push({tool,args});return {};}};
  await resumed.syncSessionTitle(session.id);await resumed.syncSessionTitle(session.id);
  assert.equal(calls.length,2);assert.equal(calls[1].tool,'set_thread_title');assert.equal(calls[1].args.threadId,'existing-thread');assert.equal(calls[1].args.title,named.title);assert.equal(store.session(session.id).applied_title,named.title);
  const renamed=store.nameSession(session.id,'新名字');assert.equal(renamed.title.replace('新名字','薯薯'),named.title);assert.notEqual(renamed.applied_title,renamed.title);store.close();await fs.rm(root,{recursive:true});
});

test('new named sessions keep their title across native creation and can recover a lost creation response by that title',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-name-native-')),s=new Store(':memory:'),task=s.create(input()).task,session=s.sessionFor(task);
  const named=s.nameSession(session.id,'薯薯'),d=new NativeDesktop(s),calls=[];
  d.ready=true;d.follow=async()=>{};d.waitForIdle=async()=>{};d.tools={call:async(tool,args)=>{calls.push({tool,args});return {threadId:'thread',projectlessOutputDirectory:root};}};
  d.ipc={request:async()=>({result:{result:{turn:{id:'turn'}}}})};
  await d.startTask(s.get(task.id));assert.equal(calls[0].args.title,named.title);assert.equal(s.session(session.id).applied_title,named.title);assert.equal(calls.length,1);
  const lost=s.create(input('lost','other')).task,other=s.sessionFor(lost),otherNamed=s.nameSession(other.id,'另一人');s.event(lost.id,'DESKTOP_RAW_REQUEST',{});
  s.event(lost.id,'DESKTOP_SESSION_CREATING',{title:otherNamed.title});s.nameSession(other.id,'用户后来改名');
  d.tools={call:async(tool,args)=>{assert.equal(tool,'list_threads');assert.equal(args.limit,50);return {threads:[{id:'recovered',title:otherNamed.title}]};}};d.stream.states.set('recovered',{revision:1,state:{projectlessOutputDirectory:root,turns:[]}});
  await d.recoverUnbound(s.get(lost.id));assert.equal(s.session(other.id).thread_id,'recovered');assert.equal(s.get(lost.id).thread_id,'recovered');s.close();await fs.rm(root,{recursive:true});
});

test('user names are looked up only for authorized tasks; missing contact permissions never block the original request',async()=>{
  const s=new Store(':memory:'),submitted=[],lookups=[],d=Object.assign(new EventEmitter(),{follow:async()=>{},startTask:async task=>{submitted.push({text:task.instruction,session:s.session(task.session_id)});return {threadId:'thread',workspace:'workspace'};}});
  const service=new TaskService(s,d,{getUserName:async user=>{lookups.push(user);if(user==='blocked')throw Error('contact scope missing');return '薯薯';}});
  const message={tenant:'tenant',user:'user',chat:'chat',message:'m',text:'  原文\n',isGroup:false};service.authorized=()=>false;await service.receive(message);assert.equal(lookups.length,0);
  service.authorized=()=>true;await service.receive(message);let task=s.list()[0];s.transition(task.id,'STARTING');await service.start(s.get(task.id));assert.equal(submitted[0].session.display_name,'薯薯');assert.equal(submitted[0].text,message.text);
  await service.receive({...message,user:'blocked',message:'blocked'});task=s.list()[1];s.transition(task.id,'STARTING');await service.start(s.get(task.id));assert.equal(submitted.length,2);assert.equal(s.get(task.id).status,'STARTING');assert.equal(submitted[1].session.title,null);s.close();
});

test('contact lookup caches only names and does not confuse an accessible user record with a returned name',async()=>{
  const client=new FeishuClient({appId:'test',appSecret:'test'}),calls=[];
  client.sdk.request=async request=>{calls.push(request);return {code:0,data:{user:request.url.includes('/missing?')?{open_id:'missing'}:{name:'薯薯',email:'do-not-cache'}}};};
  assert.equal(await client.getUserName('user'),'薯薯');assert.equal(await client.getUserName('user'),'薯薯');assert.equal(await client.getUserName('missing'),undefined);assert.equal(await client.getUserName('missing'),undefined);
  assert.equal(calls.length,2);assert.equal(calls[0].timeout,5000);assert(!JSON.stringify([...client.userNames.values()]).includes('do-not-cache'));client.close();
});
