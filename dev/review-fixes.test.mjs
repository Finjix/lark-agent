import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {DatabaseSync} from 'node:sqlite';
import {Store} from '../dist/storage/database.js';
import {Database} from '../dist/storage/sqlite.js';
import {Delivery} from '../dist/feishu/delivery.js';
import {cleanup} from '../dist/maintenance.js';

const input=(message,user='u1')=>({tenant_key:'t1',user_id:user,chat_id:'c',source_message_id:message,instruction:'任务 '+message});
const notices=s=>s.due().filter(r=>r.kind==='NOTICE').map(r=>JSON.parse(r.payload).text);

test('an image that cannot be staged is reported while the final reply is still delivered',async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'review-image-')),s=new Store(':memory:'),task=s.create(input('img')).task;
  s.transition(task.id,'COMPLETED',{workspace_path:dir,thread_id:'thr',result:'这是最终答案'});
  s.recordImages(task.id,[{id:'img1',savedPath:path.join(dir,'elsewhere.png')}]);
  const calls=[],fake={connected:true,raw:async(method,url,data)=>{calls.push({method,url,data});return url.endsWith('/cards')?{card_id:'card'}:{};},reply:async()=>({message_id:'m'})};
  const delivery=new Delivery(s,fake);await delivery.tick();
  const final=calls.filter(c=>c.method==='PUT'&&c.data.card).map(c=>JSON.parse(c.data.card.data)).at(-1);
  assert.equal(final.body.elements[0].content,'这是最终答案');
  assert.equal(s.db.prepare('SELECT status FROM delivery_outbox WHERE id=?').get(`view:${task.id}`).status,'DELIVERED');
  assert(notices(s).some(t=>t.includes('codex-image-img1.png')&&t.includes('附件未发送')));
  await delivery.tick();await delivery.tick(); // reported once, not on every pass
  assert.equal(s.db.prepare("SELECT count(*) c FROM delivery_outbox WHERE kind='NOTICE'").get().c,1);
  assert.equal(s.db.prepare("SELECT status FROM delivery_outbox WHERE kind='NOTICE'").get().status,'DELIVERED');
  await delivery.stop();s.close();await fs.rm(dir,{recursive:true,force:true});
});

test('deliveries only wait for the client to start, not for the long connection to be up',async()=>{
  const s=new Store(':memory:'),task=s.create(input('gate')).task;s.transition(task.id,'COMPLETED',{result:'完成'});let raw=0;
  const fake={started:false,connected:false,raw:async(m,url)=>{raw++;return url.endsWith('/cards')?{card_id:'c'}:{};},reply:async()=>({message_id:'m'})};
  const delivery=new Delivery(s,fake);await delivery.tick();assert.equal(raw,0);
  fake.started=true;await delivery.tick();assert(raw>0);await delivery.stop();s.close();
});

test('active() returns only unfinished work, and unfinished() the unfinished tasks of one user',()=>{
  const s=new Store(':memory:'),ids=[];
  for(let i=0;i<12;i++){const t=s.create(input('m'+i)).task;s.update(t.id,{created_at:1000+i});ids.push(t.id);}
  s.transition(ids[0],'RUNNING');s.transition(ids[1],'STARTING');s.transition(ids[2],'COMPLETED');s.transition(ids[3],'FAILED');
  assert.deepEqual(s.active().map(t=>t.id),[ids[0],ids[1]]);
  s.create(input('other','u2'));
  assert.deepEqual(s.unfinished('t1','u1').map(t=>t.id),ids.filter((_,i)=>i!==2&&i!==3),'queued and running, not finished, not another user\'s');s.close();
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

test('retention cleanup marks a task cleaned when its output directory is already gone',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'review-clean-')),s=new Store(':memory:'),task=s.create(input('old')).task;
  const session=s.sessionFor(task);s.bindSession(session.id,'thr');s.setSessionWorkspace(session.id,root);
  s.transition(task.id,'COMPLETED',{thread_id:'thr',workspace_path:path.join(root,'tasks',task.id)});
  s.update(task.id,{finished_at:1000});s.db.exec("UPDATE delivery_outbox SET status='DELIVERED'");
  await cleanup(s);assert(s.get(task.id).cleaned_at);s.close();await fs.rm(root,{recursive:true,force:true});
});
