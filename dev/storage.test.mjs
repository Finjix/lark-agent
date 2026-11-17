import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {DatabaseSync} from 'node:sqlite';
import {Store} from '../dist/storage/database.js';
import {Database} from '../dist/storage/sqlite.js';

function input(message='m1',user='u1',tenant='t1'){return {tenant_key:tenant,user_id:user,chat_id:'chat-'+user,source_message_id:message,instruction:'任务 '+message};}
function memory(){return new Store(':memory:');}

test('transaction rolls back on error and nests through savepoints',()=>{
  const db=new Database(':memory:');db.exec('CREATE TABLE t(a)');
  const insert=value=>db.prepare('INSERT INTO t VALUES(?)').run(value);
  assert.throws(()=>db.transaction(()=>{insert(1);throw Error('boom');})());
  assert.equal(db.prepare('SELECT COUNT(*) AS n FROM t').get().n,0);
  db.transaction(()=>{insert(1);assert.throws(()=>db.transaction(()=>{insert(2);throw Error('inner');})());insert(3);})();
  assert.deepEqual(db.prepare('SELECT a FROM t ORDER BY a').all().map(r=>r.a),[1,3]);
  assert.equal(db.pragma('user_version',{simple:true}),0);db.close();
});

test('a lock timeout on BEGIN does not leave later transactions half-nested',async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'review-lock-')),file=path.join(dir,'t.sqlite');
  const holder=new DatabaseSync(file);holder.exec('PRAGMA journal_mode=WAL;CREATE TABLE x(v);BEGIN IMMEDIATE');
  const db=new Database(file);db.pragma('busy_timeout = 20');
  assert.throws(()=>db.transaction(()=>1)(),/locked/);assert.equal(db.depth,0);
  holder.exec('COMMIT');holder.close();
  db.transaction(()=>{db.exec('INSERT INTO x VALUES (1)');db.transaction(()=>db.exec('INSERT INTO x VALUES (2)'))();})();
  assert.equal(db.prepare('SELECT count(*) c FROM x').get().c,2);db.close();await fs.rm(dir,{recursive:true,force:true});
});

test('SQLite deduplicates within tenant, preserves recovery links and leaves a newer outbox generation pending',async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'lark-db-'));const file=path.join(dir,'test.sqlite');let s=new Store(file);
  const a=s.create(input());assert(a.created);assert.equal(s.create(input()).created,false);assert(s.create(input('m1','u1','t2')).created);
  s.transition(a.task.id,'STARTING',{thread_id:'thread'});
  const old=s.due().find(x=>x.task_id===a.task.id);s.enqueue(old.id,a.task.id,'VIEW',{});s.ack(old.id,old.version);
  assert(s.due().some(x=>x.id===old.id));
  s.transition(a.task.id,'COMPLETED',{result:'完成'});s.close();s=new Store(file);
  assert.equal(s.get(a.task.id).thread_id,'thread');assert.equal(s.get(a.task.id).status,'COMPLETED');assert(s.due().some(x=>x.task_id===a.task.id));
  s.transition(a.task.id,'RUNNING');assert.equal(s.get(a.task.id).status,'COMPLETED');s.close();await fs.rm(dir,{recursive:true});
});

test('terminal transition and final outbox are atomic on database failure',()=>{
  const s=memory(),t=s.create(input()).task;
  s.db.exec("CREATE TRIGGER fail_outbox BEFORE UPDATE ON delivery_outbox BEGIN SELECT RAISE(ABORT,'disk failure'); END;");
  assert.throws(()=>s.transition(t.id,'COMPLETED',{result:'ok'}));assert.equal(s.get(t.id).status,'QUEUED');s.close();
});

test('active() returns only unfinished work, and unfinished() the unfinished tasks of one user',()=>{
  const s=new Store(':memory:'),ids=[];
  for(let i=0;i<12;i++){const t=s.create(input('m'+i)).task;s.update(t.id,{created_at:1000+i});ids.push(t.id);}
  s.transition(ids[0],'RUNNING');s.transition(ids[1],'STARTING');s.transition(ids[2],'COMPLETED');s.transition(ids[3],'FAILED');
  assert.deepEqual(s.active().map(t=>t.id),[ids[0],ids[1]]);
  s.create(input('other','u2'));
  assert.deepEqual(s.unfinished('t1','u1').map(t=>t.id),ids.filter((_,i)=>i!==2&&i!==3),'queued and running, not finished, not another user\'s');s.close();
});
