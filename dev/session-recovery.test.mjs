import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {Database} from '../dist/storage/sqlite.js';
import {Store} from '../dist/storage/database.js';

const input=(message='one',user='user',tenant='tenant')=>({tenant_key:tenant,user_id:user,chat_id:'chat',source_message_id:message,instruction:message});
const columns=(db,table)=>db.prepare(`PRAGMA table_info(${table})`).all().map(c=>c.name);
const indexes=db=>db.prepare("SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%' ORDER BY name").all().map(r=>r.name);
const tables=db=>db.prepare("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").all().map(r=>r.name);
const CURRENT={
  tasks:['id','tenant_key','user_id','chat_id','source_message_id','instruction','thread_id','workspace_path','status','result','error','progress','created_at','started_at','finished_at','cancel_requested','resources','cleaned_at','session_id','steps'],
  task_messages:['task_id','message_id','card_id','sequence','finalized'],
  task_generated_images:['task_id','item_id','saved_path'],
  user_sessions:['id','tenant_key','user_id','thread_id','workspace_path','created_at','display_name','retired_at'],
};

test('a new database is at the current version and opening it again changes nothing',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-db-fresh-')),file=path.join(root,'agent.sqlite');
  try{
    let s=new Store(file);const task=s.create(input()).task,session=s.sessionFor(task);s.bindSession(session.id,'thread');
    assert.equal(s.db.pragma('user_version',{simple:true}),2);
    for(const [table,list] of Object.entries(CURRENT))assert.deepEqual(columns(s.db,table),list,table);
    assert.deepEqual(tables(s.db),['delivery_outbox','task_artifacts','task_events','task_generated_images','task_inputs','task_messages','tasks','user_dirs','user_sessions']);
    assert.deepEqual(indexes(s.db),['current_user_session','outbox_due','task_event_lookup','task_queue','task_user']);
    const schema=()=>s.db.prepare('SELECT name,sql FROM sqlite_master ORDER BY name').all();const before=schema();s.close();
    s=new Store(file);assert.deepEqual(schema(),before);assert.equal(s.session(session.id).retired_at,null,'a current session is not retired by opening the database');
    assert.deepEqual(s.db.pragma('foreign_key_check'),[]);assert.equal(s.db.pragma('foreign_keys',{simple:true}),1);s.close();
  }finally{await fs.rm(root,{recursive:true,force:true});}
});

test('a database of another version is refused and left exactly as it was',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-db-version-'));
  try{
    for(const version of [3,6,7]){
      const file=path.join(root,`v${version}.sqlite`),other=new Database(file);
      other.exec(`CREATE TABLE tasks (id TEXT PRIMARY KEY); INSERT INTO tasks VALUES('a'); PRAGMA user_version=${version}`);other.close();
      const bytes=await fs.readFile(file);
      assert.throws(()=>new Store(file),error=>{assert.match(error.message,new RegExp(`user_version=${version}`));assert(error.message.includes(file),'says which database');return true;},`version ${version}`);
      assert.deepEqual(await fs.readFile(file),bytes,`version ${version}: not a byte changed`);
      assert.deepEqual((await fs.readdir(root)).filter(name=>name.startsWith(`v${version}.sqlite-`)),[],'no journal or WAL left behind');
    }
    // An empty file is a new database.
    const empty=new Database(path.join(root,'empty.sqlite'));empty.close();const created=new Store(path.join(root,'empty.sqlite'));assert.equal(created.db.pragma('user_version',{simple:true}),2);created.close();
  }finally{await fs.rm(root,{recursive:true,force:true});}
});

test('a database of version 1 (from before STEERING/STEERED) is taken over as it is; from then on a program of version 1 refuses it',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-db-v1-')),file=path.join(root,'agent.sqlite');
  try{
    let s=new Store(file);const task=s.create(input()).task;s.db.exec('PRAGMA user_version=1');s.close();
    s=new Store(file);assert.equal(s.db.pragma('user_version',{simple:true}),2);assert.equal(s.get(task.id).status,'QUEUED','its tasks are kept');s.close();
  }finally{await fs.rm(root,{recursive:true,force:true});}
});

test('a replaced chat stays replaced after a restart: the new session is the current one and the old one is kept for the tasks that ran in it',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-session-restart-')),file=path.join(root,'agent.sqlite');let s=new Store(file);
  try{
    const old=s.create(input('old')).task,first=s.sessionFor(old);s.bindSession(first.id,'old-thread');s.setSessionWorkspace(first.id,'old-workspace');s.transition(old.id,'COMPLETED');
    const pending=s.create(input('pending')).task;s.sessionFor(pending);const next=s.renewSession(pending,'missing');s.close();
    s=new Store(file);assert.equal(s.sessionFor(s.get(pending.id)).id,next.id);assert.equal(s.get(old.id).session_id,first.id);
    assert.equal(s.session(first.id).thread_id,'old-thread');assert(s.session(first.id).retired_at);assert.equal(s.session(next.id).retired_at,null);
    assert.deepEqual(s.db.pragma('foreign_key_check'),[]);assert.equal(s.db.pragma('foreign_keys',{simple:true}),1);
  }finally{s.close();await fs.rm(root,{recursive:true,force:true});}
});
