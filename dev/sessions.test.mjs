import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {Store} from '../dist/storage/database.js';
import {taskWorkspace} from '../dist/layout.js';
import {cleanup} from '../dist/maintenance.js';

const input=(message='one',user='user',tenant='tenant')=>({tenant_key:tenant,user_id:user,chat_id:'chat',source_message_id:message,instruction:message});
/** A user's session bound to a Codex thread, working in `root`. */
function bound(s,task,thread,root){const session=s.sessionFor(task);s.bindSession(session.id,thread);if(root)s.setSessionWorkspace(session.id,root);return s.session(session.id);}

test('session ownership survives restart and is isolated by tenant and user',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-session-db-')),file=path.join(root,'db.sqlite');let s=new Store(file);
  const a=s.create(input()).task,b=s.create(input('two')).task,c=s.create(input('other-one','other')).task,d=s.create(input('one','user','other-tenant')).task;
  const session=bound(s,a,'thread','workspace');
  assert.equal(s.sessionFor(b).id,session.id);assert.notEqual(s.sessionFor(c).id,session.id);assert.notEqual(s.sessionFor(d).id,session.id);
  assert.throws(()=>s.bindSession(session.id,'wrong-thread'),/其他/);
  s.close();s=new Store(file);assert.equal(s.sessionFor(s.get(b.id)).thread_id,'thread');assert.equal(s.get(b.id).session_id,session.id);
  s.close();await fs.rm(root,{recursive:true});
});

test('a chat replaced or reset keeps its history on the old session and is never shared between two users',()=>{
  const s=new Store(':memory:'),a=s.create(input()).task,other=s.create(input('x','other')).task;
  const first=bound(s,a,'thread-1','w');s.nameSession(first.id,'薯薯');
  const next=s.renewSession(s.get(a.id),'missing');
  assert.notEqual(next.id,first.id);assert.equal(next.thread_id,null);assert.equal(next.display_name,'薯薯','the name moves to the new session');
  assert.equal(s.session(first.id).thread_id,'thread-1');assert(s.session(first.id).retired_at);assert.equal(s.get(a.id).session_id,next.id);assert.equal(s.get(a.id).thread_id,null);
  assert.equal(s.sessions().length,1);
  assert.equal(s.resetSession('tenant','other'),false,'nothing to forget before the first chat');s.sessionFor(other);assert.equal(s.resetSession('tenant','other'),false,'a session without a chat either');
  bound(s,other,'thread-2');assert.equal(s.resetSession('tenant','other'),true);
  assert.equal(s.sessionFor(s.get(other.id)).thread_id,null);assert.equal(s.session(s.get(other.id).session_id).display_name,null);
  assert.deepEqual(s.db.pragma('foreign_key_check'),[]);s.close();
});

test('expiring a completed message removes only the files it delivered and keeps the output folder, the session and later files',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-session-retain-')),s=new Store(':memory:');
  const old=s.create(input()).task,session=bound(s,old,'thread',root);
  const output=taskWorkspace(root);await fs.mkdir(output,{recursive:true});
  const oldFile=path.join(output,'old.txt'),laterFile=path.join(output,'later.txt');await fs.writeFile(oldFile,'old');await fs.writeFile(laterFile,'keep');
  s.transition(old.id,'COMPLETED',{thread_id:'thread',workspace_path:output});s.update(old.id,{finished_at:1});
  s.db.prepare('INSERT INTO task_artifacts(id,task_id,path,name,size,kind) VALUES(?,?,?,?,?,?)').run('a1',old.id,oldFile,'old.txt',3,'file');
  const row=s.due()[0];s.ack(row.id,row.version);
  await cleanup(s);
  assert(s.get(old.id).cleaned_at);assert.equal(s.session(session.id).thread_id,'thread');
  await assert.rejects(fs.stat(oldFile),{code:'ENOENT'});assert.equal(await fs.readFile(laterFile,'utf8'),'keep');assert((await fs.stat(output)).isDirectory());
  s.close();await fs.rm(root,{recursive:true});
});

test('a task recorded under the earlier folder name output is still cleaned up, and only its own files go',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-session-legacy-')),s=new Store(':memory:');
  const old=s.create(input()).task;bound(s,old,'thread',root);
  const output=path.join(root,'output');await fs.mkdir(output,{recursive:true});
  const oldFile=path.join(output,'old.txt'),laterFile=path.join(output,'later.txt');await fs.writeFile(oldFile,'old');await fs.writeFile(laterFile,'keep');
  s.transition(old.id,'COMPLETED',{thread_id:'thread',workspace_path:output});s.update(old.id,{finished_at:1});
  s.db.prepare('INSERT INTO task_artifacts(id,task_id,path,name,size,kind) VALUES(?,?,?,?,?,?)').run('a1',old.id,oldFile,'old.txt',3,'file');
  const row=s.due()[0];s.ack(row.id,row.version);
  await cleanup(s);
  assert(s.get(old.id).cleaned_at);await assert.rejects(fs.stat(oldFile),{code:'ENOENT'});assert.equal(await fs.readFile(laterFile,'utf8'),'keep');
  s.close();await fs.rm(root,{recursive:true});
});

test('files of a Desktop-era session are still found through that session, and only inside its own chat folder',async()=>{
  // The old layout kept a chat's files in Documents\Codex\<date>\lark-<id>; that session is retired but its record says where.
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-session-desktop-era-')),s=new Store(':memory:');
  const old=s.create(input()).task,session=bound(s,old,'old-thread',root);
  const output=taskWorkspace(root),mine=path.join(output,'mine.txt'),foreign=path.join(root,'..',`foreign-${process.pid}.txt`);
  await fs.mkdir(output,{recursive:true});await fs.writeFile(mine,'x');await fs.writeFile(foreign,'keep');
  s.transition(old.id,'COMPLETED',{thread_id:'old-thread',workspace_path:output});s.update(old.id,{finished_at:1});
  s.db.prepare('INSERT INTO task_artifacts(id,task_id,path,name,size,kind) VALUES(?,?,?,?,?,?)').run('a1',old.id,mine,'mine.txt',1,'file');
  s.db.prepare('INSERT INTO task_artifacts(id,task_id,path,name,size,kind) VALUES(?,?,?,?,?,?)').run('a2',old.id,foreign,'foreign.txt',4,'file');
  s.db.prepare('UPDATE user_sessions SET retired_at=1 WHERE id=?').run(session.id);const row=s.due()[0];s.ack(row.id,row.version);
  await cleanup(s);
  await assert.rejects(fs.stat(mine),{code:'ENOENT'});assert.equal(await fs.readFile(foreign,'utf8'),'keep','a path outside the folder is never removed');
  await fs.rm(foreign);s.close();await fs.rm(root,{recursive:true});
});
