import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {Database} from '../dist/storage/sqlite.js';
import {EventEmitter} from 'node:events';
import {Store} from '../dist/storage/database.js';
import {NativeDesktop,SESSION_INIT_PROMPT} from '../dist/desktop/adapter.js';
import {desktopThreadStatus} from '../dist/desktop/context.js';
import {TaskService} from '../dist/tasks/service.js';
import {StreamState} from '../dist/desktop/stream.js';

const input=(message='one',user='user')=>({tenant_key:'tenant',user_id:user,chat_id:'chat',source_message_id:message,instruction:message});

test('thread lookup distinguishes archived, absent and active threads without treating an unreadable index as missing',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-thread-state-'));
  try{
    const db=new Database(path.join(root,'state_10.sqlite'));
    db.exec("CREATE TABLE threads(id TEXT,archived INTEGER); INSERT INTO threads VALUES('active',0),('archived',1)");db.close();
    assert.equal(desktopThreadStatus('active',root),'active');assert.equal(desktopThreadStatus('archived',root),'archived');
    assert.equal(desktopThreadStatus('deleted',root),'missing');
    await fs.mkdir(path.join(root,'empty'));assert.throws(()=>desktopThreadStatus('active',path.join(root,'empty')),/索引/);
  }finally{await fs.rm(root,{recursive:true});}
});

for(const status of ['archived','missing'])test(`${status} sessions create a fresh thread before sending pending raw input; subsequent input reuses it`,async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-session-replace-')),s=new Store(':memory:');
  try{
    const old=s.create(input('old')).task,session=s.sessionFor(old);
    s.bindSession(session.id,'old-thread',path.join(root,'old'));s.nameSession(session.id,'薯薯');
    s.transition(old.id,'COMPLETED',{thread_id:'old-thread',turn_id:'old-turn',workspace_path:path.join(root,'old-task'),result:'old reply'});
    const calls=[],followed=[],d=new NativeDesktop(s,id=>id==='old-thread'?status:'active');
    d.ready=true;d.follow=async id=>followed.push(id);d.waitForIdle=async()=>{};
    d.tools={call:async(name,args)=>{calls.push({name,args});return {threadId:'new-thread',projectlessOutputDirectory:path.join(root,'new')};}};
    d.ipc={request:async(name,args)=>{calls.push({name,args});return {result:{result:{turn:{id:`turn-${calls.length}`}}}};}};
    const next=s.create(input('  new\n')).task;
    const created=await d.startTask(next),active=s.sessionFor(s.get(next.id));
    assert.equal(created.threadId,'new-thread');assert.notEqual(active.id,session.id);assert.equal(active.display_name,'薯薯');
    assert.equal(s.session(session.id).thread_id,'old-thread');assert(s.session(session.id).retired_at);
    assert.equal(s.get(old.id).session_id,session.id);assert.equal(s.get(old.id).result,'old reply');
    assert.deepEqual(followed,['new-thread']);assert.equal(calls[0].name,'create_thread');assert.equal(calls[0].args.prompt,SESSION_INIT_PROMPT);
    assert.equal(calls[1].args.turnStart.request.input[0].text,'  new\n');
    const later=s.create(input('later')).task;await d.startTask(later);
    assert.equal(calls.filter(c=>c.name==='create_thread').length,1);assert.equal(s.sessionFor(later).id,active.id);
    assert.deepEqual(s.db.pragma('foreign_key_check'),[]);
  }finally{s.close();await fs.rm(root,{recursive:true});}
});

test('pre-send recovery releases the queue; uncertain sent input never rotates or resubmits',async()=>{
  const s=new Store(':memory:'),followed=[],d=Object.assign(new EventEmitter(),{stream:new StreamState(),follow:async id=>{followed.push(id);throw Error('no-client-found');}}),service=new TaskService(s,d);
  try{
    for(const message of ['not-sent','sent']){
      const t=s.create(input(message,message)).task,session=s.sessionFor(t);s.bindSession(session.id,message,'workspace');
      s.transition(t.id,'UNKNOWN',{thread_id:message});s.event(t.id,'DESKTOP_RAW_REQUEST',{});
      if(message==='sent')s.event(t.id,'DESKTOP_TURN_SUBMITTING',{clientUserMessageId:t.id});
    }
    await service.reconcile();assert.deepEqual(s.list().map(t=>t.status),['QUEUED','UNKNOWN']);assert.deepEqual(followed,['sent']);
    const task=s.list()[1],native=new NativeDesktop(s,()=> 'archived');native.ready=true;native.tools={};native.ipc={};
    await assert.rejects(native.startTask(task),/不能重复发送/);assert.throws(()=>s.renewSession(task,'archived'),/不能切换/);
    assert.equal(s.sessions().length,2);
  }finally{s.close();}
});

test('version 4 migration preserves historical task references and permits a new current session after restart',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-session-v4-')),file=path.join(root,'agent.sqlite');let s=new Store(file);
  const old=s.create(input('old')).task,session=s.sessionFor(old);s.bindSession(session.id,'old-thread','old-workspace');s.transition(old.id,'COMPLETED');s.close();
  const legacy=new Database(file);legacy.pragma('foreign_keys=OFF');legacy.exec(`
    CREATE TABLE legacy_sessions(id TEXT PRIMARY KEY,tenant_key TEXT NOT NULL,user_id TEXT NOT NULL,thread_id TEXT UNIQUE,
      workspace_path TEXT,created_at INTEGER NOT NULL,display_name TEXT,title TEXT,applied_title TEXT,UNIQUE(tenant_key,user_id));
    INSERT INTO legacy_sessions SELECT id,tenant_key,user_id,thread_id,workspace_path,created_at,display_name,title,applied_title FROM user_sessions;
    DROP TABLE user_sessions; ALTER TABLE legacy_sessions RENAME TO user_sessions; PRAGMA user_version=4;
  `);legacy.close();
  try{
    s=new Store(file);const pending=s.create(input('pending')).task;s.sessionFor(pending);const next=s.renewSession(pending,'archived');s.close();
    s=new Store(file);assert.equal(s.sessionFor(s.get(pending.id)).id,next.id);assert.equal(s.get(old.id).session_id,session.id);
    assert.equal(s.session(session.id).thread_id,'old-thread');assert.deepEqual(s.db.pragma('foreign_key_check'),[]);
    assert.equal(s.db.pragma('foreign_keys',{simple:true}),1);
  }finally{s.close();await fs.rm(root,{recursive:true});}
});
