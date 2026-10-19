import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {Database} from '../dist/storage/sqlite.js';
import {Store} from '../dist/storage/database.js';
import {BOT_DOWN} from '../dist/types.js';

const input=(message='one',user='user')=>({tenant_key:'tenant',user_id:user,chat_id:'chat',source_message_id:message,instruction:message});
const columns=(db,table)=>db.prepare(`PRAGMA table_info(${table})`).all().map(c=>c.name);

test('a new database is at the current version and opening it again changes nothing',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-db-fresh-')),file=path.join(root,'agent.sqlite');
  try{
    let s=new Store(file);const task=s.create(input()).task,session=s.sessionFor(task);s.bindSession(session.id,'thread');
    assert.equal(s.db.pragma('user_version',{simple:true}),6);
    assert.deepEqual(columns(s.db,'user_sessions'),['id','tenant_key','user_id','thread_id','workspace_path','created_at','display_name','retired_at']);
    assert.equal(s.db.prepare("SELECT count(*) c FROM sqlite_master WHERE name='pending_requests'").get().c,0,'there are no approvals any more');
    const schema=()=>s.db.prepare('SELECT name,sql FROM sqlite_master ORDER BY name').all();const before=schema();s.close();
    s=new Store(file);assert.deepEqual(schema(),before);assert.equal(s.session(session.id).retired_at,null,'a current session is not retired by opening the database');
    assert.deepEqual(s.db.pragma('foreign_key_check'),[]);s.close();
  }finally{await fs.rm(root,{recursive:true,force:true});}
});

test('version 4 migration preserves historical task references and permits a new current session after restart',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-session-v4-')),file=path.join(root,'agent.sqlite');let s=new Store(file);
  const old=s.create(input('old')).task,session=s.sessionFor(old);s.bindSession(session.id,'old-thread');s.setSessionWorkspace(session.id,'old-workspace');s.transition(old.id,'COMPLETED');s.close();
  const legacy=new Database(file);legacy.pragma('foreign_keys=OFF');legacy.exec(`
    CREATE TABLE legacy_sessions(id TEXT PRIMARY KEY,tenant_key TEXT NOT NULL,user_id TEXT NOT NULL,thread_id TEXT UNIQUE,
      workspace_path TEXT,created_at INTEGER NOT NULL,display_name TEXT,title TEXT,applied_title TEXT,UNIQUE(tenant_key,user_id));
    INSERT INTO legacy_sessions(id,tenant_key,user_id,thread_id,workspace_path,created_at) SELECT id,tenant_key,user_id,thread_id,workspace_path,created_at FROM user_sessions;
    DROP TABLE user_sessions; ALTER TABLE legacy_sessions RENAME TO user_sessions; PRAGMA user_version=4;
  `);legacy.close();
  try{
    s=new Store(file);const pending=s.create(input('pending')).task;s.sessionFor(pending);const next=s.renewSession(pending,'missing');s.close();
    s=new Store(file);assert.equal(s.sessionFor(s.get(pending.id)).id,next.id);assert.equal(s.get(old.id).session_id,session.id);
    assert.equal(s.session(session.id).thread_id,'old-thread');assert.deepEqual(s.db.pragma('foreign_key_check'),[]);
    assert.equal(s.db.pragma('foreign_keys',{simple:true}),1);
    assert(s.session(session.id).retired_at,'a chat of the Desktop bridge is not continued');
  }finally{s.close();await fs.rm(root,{recursive:true});}
});

test('a database of the Desktop bridge is taken over: its chats are set aside, what was running is failed at start, nothing else is lost',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-session-v5-')),file=path.join(root,'agent.sqlite');let s=new Store(file);
  const done=s.create(input('done','a')).task,running=s.create(input('running','b')).task,fresh=s.create(input('fresh','c')).task;
  const chat=s.sessionFor(done),named=s.sessionFor(running),empty=s.sessionFor(fresh);
  s.bindSession(chat.id,'desktop-thread-a');s.bindSession(named.id,'desktop-thread-b');s.setSessionWorkspace(chat.id,'C:/Users/x/Documents/Codex/2026-10-16/lark-a');s.nameSession(named.id,'薯薯');
  s.transition(done.id,'COMPLETED',{result:'旧结果',thread_id:'desktop-thread-a'});s.transition(running.id,'UNKNOWN',{thread_id:'desktop-thread-b'});
  s.userDirName('tenant','b','薯薯');s.close();
  // What the version before looked like: titles on sessions, a table of approvals, the old version number.
  const db=new Database(file);db.exec(`ALTER TABLE user_sessions ADD COLUMN title TEXT; ALTER TABLE user_sessions ADD COLUMN applied_title TEXT;
    CREATE TABLE pending_requests (task_id TEXT NOT NULL REFERENCES tasks(id),request_id TEXT NOT NULL,kind TEXT NOT NULL,summary TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'PENDING',created_at INTEGER NOT NULL,expires_at INTEGER NOT NULL,PRIMARY KEY(task_id,request_id));
    UPDATE user_sessions SET title='薯薯_2026_1016_1700_0000',applied_title='薯薯_2026_1016_1700_0000'; PRAGMA user_version=5;`);db.close();
  try{
    s=new Store(file);
    assert.equal(s.db.pragma('user_version',{simple:true}),6);assert.deepEqual(columns(s.db,'user_sessions'),['id','tenant_key','user_id','thread_id','workspace_path','created_at','display_name','retired_at']);
    assert.equal(s.db.prepare("SELECT count(*) c FROM sqlite_master WHERE name='pending_requests'").get().c,0);
    assert(s.session(chat.id).retired_at,'a Desktop chat cannot be resumed from the Codex home of this project');assert.equal(s.session(chat.id).thread_id,'desktop-thread-a','but it stays on record');
    assert(s.session(named.id).retired_at);assert.equal(s.session(empty.id).retired_at,null,'a session that never had a chat is kept');
    assert.equal(s.session(named.id).display_name,'薯薯');assert.equal(s.userDirName('tenant','b','别的名字'),'薯薯','the user\'s folder keeps its name, so browser and Feishu logins survive');
    assert.equal(s.get(done.id).result,'旧结果');assert.equal(s.get(done.id).session_id,chat.id);
    // The next input of a user with a Desktop chat is a new chat.
    assert.equal(s.sessionFor(s.create(input('again','a')).task).thread_id,null);
    // The task that was running when the old service stopped is over: nothing is left to resume it.
    s.discardLeftovers();assert.equal(s.get(running.id).status,'FAILED');assert.equal(s.get(running.id).error,BOT_DOWN);
    assert.deepEqual(s.db.pragma('foreign_key_check'),[]);
  }finally{s.close();await fs.rm(root,{recursive:true});}
});
