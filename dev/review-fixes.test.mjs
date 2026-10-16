import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {EventEmitter} from 'node:events';
import {DatabaseSync} from 'node:sqlite';
import {Store} from '../dist/storage/database.js';
import {Database} from '../dist/storage/sqlite.js';
import {FrameDecoder,encodeFrame,NotSubmitted,RpcRejected,OutcomeUnknown} from '../dist/desktop/transport.js';
import {NativeDesktop} from '../dist/desktop/adapter.js';
import {TaskService} from '../dist/tasks/service.js';
import {Delivery} from '../dist/feishu/delivery.js';
import {cleanup} from '../dist/maintenance.js';

const input=(message,user='u1')=>({tenant_key:'t1',user_id:user,chat_id:'c',source_message_id:message,instruction:'任务 '+message});
const msg=(text,message='cmd')=>({tenant:'t1',user:'u1',chat:'c',message,text});
const notices=s=>s.due().filter(r=>r.kind==='NOTICE').map(r=>JSON.parse(r.payload).text);
// A user whose Desktop chat already exists, plus a fresh task that is about to start.
function boundUser(s){
  const first=s.create(input('first')).task,session=s.sessionFor(first);
  s.bindSession(session.id,'thread-1',path.join(os.tmpdir(),'review-session'));s.transition(first.id,'COMPLETED');
  const task=s.create(input('second')).task;s.transition(task.id,'STARTING',{started_at:Date.now()});return task;
}

test('frame decoder joins a large frame once and handles a header split across chunks',()=>{
  const payload={text:'x'.repeat(300000),n:'中文'},frame=encodeFrame(payload),decoder=new FrameDecoder();
  const out=[];for(let i=0;i<frame.length;i+=1000)out.push(...decoder.push(frame.subarray(i,i+1000)));
  assert.deepEqual(out,[payload]);
  const two=Buffer.concat([encodeFrame({a:1}),encodeFrame({b:2})]),d2=new FrameDecoder(),got=[];
  for(const part of [two.subarray(0,2),two.subarray(2,5),two.subarray(5)])got.push(...d2.push(part));
  assert.deepEqual(got,[{a:1},{b:2}]);
});

test('Desktop not ready at submission puts the task back in the queue instead of stranding it as UNKNOWN',async()=>{
  const s=new Store(':memory:'),task=boundUser(s),d=new NativeDesktop(s,()=>'active'),service=new TaskService(s,d);
  await service.start(s.get(task.id));
  assert.equal(s.get(task.id).status,'QUEUED');assert.equal(s.get(task.id).turn_id,null);
  // A cancel that arrived while it was starting wins over re-queueing.
  const other=s.create(input('third','u2')).task;s.transition(other.id,'STARTING');s.update(other.id,{cancel_requested:1});
  await service.start(s.get(other.id));assert.equal(s.get(other.id).status,'CANCELLED');s.close();
});

test('an explicit Desktop rejection of the business request fails the task, an unknown outcome does not',async()=>{
  const s=new Store(':memory:'),task=boundUser(s),d=Object.assign(new EventEmitter(),{follow:async()=>{}}),service=new TaskService(s,d);
  d.startTask=async t=>{s.event(t.id,'DESKTOP_TURN_SUBMITTING',{clientUserMessageId:t.id});throw new RpcRejected('thread busy');};
  await service.start(s.get(task.id));assert.equal(s.get(task.id).status,'FAILED');assert.match(s.get(task.id).error,/任务未执行/);
  const next=s.create(input('third')).task;s.transition(next.id,'STARTING');
  d.startTask=async t=>{s.event(t.id,'DESKTOP_TURN_SUBMITTING',{clientUserMessageId:t.id});throw new OutcomeUnknown('lost');};
  await service.start(s.get(next.id));assert.equal(s.get(next.id).status,'UNKNOWN');s.close();
});

test('reconcile releases a task stranded by an earlier failure before any send was attempted',async()=>{
  const s=new Store(':memory:'),task=boundUser(s),d=Object.assign(new EventEmitter(),{follow:async()=>{}}),service=new TaskService(s,d);
  const session=s.sessionFor(s.get(task.id));
  // The state older versions left behind: UNKNOWN, thread attached, no RAW_REQUEST and no submission.
  s.transition(task.id,'UNKNOWN',{thread_id:session.thread_id,error:'需要核对 Desktop：Desktop 尚未就绪'});
  await service.reconcile();assert.equal(s.get(task.id).status,'QUEUED');
  // Once a send was attempted, the task must stay UNKNOWN.
  s.transition(task.id,'UNKNOWN');s.event(task.id,'DESKTOP_TURN_SUBMITTING',{clientUserMessageId:task.id});
  await service.reconcile();assert.equal(s.get(task.id).status,'UNKNOWN');s.close();
});

test('/cancel ends an UNKNOWN task that Desktop never confirmed and frees the user queue',async()=>{
  const s=new Store(':memory:'),task=boundUser(s),service=new TaskService(s,new EventEmitter());
  s.transition(task.id,'UNKNOWN',{thread_id:'thread-1'});
  await service.command(msg(`/cancel ${task.id}`));
  assert.equal(s.get(task.id).status,'CANCELLED');assert.deepEqual(notices(s),['任务已取消。']);
  // A task whose request may have reached Desktop says so; one with a confirmed turn still goes through interrupt.
  const sent=s.create(input('sent')).task;s.transition(sent.id,'UNKNOWN');s.event(sent.id,'DESKTOP_TURN_SUBMITTING',{clientUserMessageId:sent.id});
  await service.command(msg(`/cancel ${sent.id}`,'cmd2'));assert.equal(s.get(sent.id).status,'CANCELLED');
  assert(notices(s).some(t=>t.includes('仍可能继续执行')));
  const running=s.create(input('running')).task;s.transition(running.id,'RUNNING',{thread_id:'th',turn_id:'tu'});
  service.desktop.interrupt=async()=>{};await service.command(msg(`/cancel ${running.id}`,'cmd3'));
  assert.equal(s.get(running.id).status,'RUNNING');assert.equal(s.get(running.id).cancel_requested,1);s.close();
});

test('a failed interrupt can be retried instead of being remembered as sent',async()=>{
  const s=new Store(':memory:');let calls=0;
  const d=Object.assign(new EventEmitter(),{interrupt:async()=>{if(++calls===1)throw new Error('timed out');}}),service=new TaskService(s,d);
  const task={id:'x',thread_id:'th',turn_id:'tu'};
  await assert.rejects(service.interrupt(task),/timed out/);await service.interrupt(task);await service.interrupt(task);
  assert.equal(calls,2);s.close();
});

test('free-text answers that look like JSON scalars are delivered as answers',async()=>{
  const s=new Store(':memory:'),d=new NativeDesktop(s,()=>'active'),sent=[];d.ipc={request:async(m,p)=>{sent.push(p.response);return {};}};
  const request={id:'7',kind:'input',summary:'',payload:{questions:[{id:'q1'}]}};
  for(const answer of ['42','true','null','"quoted"','hello'])await d.respond('th','tu',request,answer);
  assert.deepEqual(sent.map(r=>r.answers.q1.answers[0]),['42','true','null','"quoted"','hello']);
  await d.respond('th','tu',{...request,payload:{questions:[{id:'a'},{id:'b'}]}},'{"a":"1","b":"2"}');
  assert.deepEqual(sent.at(-1).answers,{a:{answers:['1']},b:{answers:['2']}});
  await assert.rejects(d.respond('th','tu',{...request,payload:{questions:[{id:'a'},{id:'b'}]}},'42'),/多问题/);
  await assert.rejects(d.respond('th','tu',{id:'8',kind:'elicitation',summary:'',payload:{}},'not json'),/JSON/);s.close();
});

test('/approve and /deny never answer a question request with their own keyword',async()=>{
  const s=new Store(':memory:'),task=s.create(input('q')).task;s.transition(task.id,'WAITING_APPROVAL',{thread_id:'th',turn_id:'tu'});
  s.setRequests(task.id,[{id:'5',kind:'input',summary:'问题',payload:{questions:[{id:'q1'}]}}]);
  let responded=0;const d=Object.assign(new EventEmitter(),{respond:async()=>{responded++;}}),service=new TaskService(s,d);
  await service.command(msg(`/deny ${task.id} 5`));await service.command(msg(`/approve ${task.id} 5`,'cmd2'));
  assert.equal(responded,0);assert(notices(s).every(t=>t.includes('/answer')));
  await service.command(msg(`/answer ${task.id} 5 好的`,'cmd3'));assert.equal(responded,1);s.close();
});

test('an image that cannot be staged is reported while the final reply is still delivered',async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'review-image-')),s=new Store(':memory:'),task=s.create(input('img')).task;
  s.transition(task.id,'COMPLETED',{workspace_path:dir,thread_id:'thr',result:'这是最终答案'});
  s.recordImages(task.id,[{id:'img1',savedPath:path.join(dir,'elsewhere.png'),base64:null}]);
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

test('active() returns only unfinished work, and recent() the newest tasks of one user',()=>{
  const s=new Store(':memory:'),ids=[];
  for(let i=0;i<12;i++){const t=s.create(input('m'+i)).task;s.update(t.id,{created_at:1000+i});ids.push(t.id);}
  s.transition(ids[0],'RUNNING');s.transition(ids[1],'UNKNOWN');s.transition(ids[2],'COMPLETED');s.transition(ids[3],'FAILED');
  assert.deepEqual(s.active().map(t=>t.id),[ids[0],ids[1]]);
  s.create(input('other','u2'));
  assert.deepEqual(s.recent('t1','u1',10).map(t=>t.id),ids.slice(2).reverse());s.close();
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
  const session=s.sessionFor(task);s.bindSession(session.id,'thr',root);
  s.transition(task.id,'COMPLETED',{thread_id:'thr',workspace_path:path.join(root,'tasks',task.id)});
  s.update(task.id,{finished_at:1000});s.db.exec("UPDATE delivery_outbox SET status='DELIVERED'");
  await cleanup(s,{});assert(s.get(task.id).cleaned_at);s.close();await fs.rm(root,{recursive:true,force:true});
});
