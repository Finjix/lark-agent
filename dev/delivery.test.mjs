import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {Store} from '../dist/storage/database.js';
import {Delivery,RateGate} from '../dist/feishu/delivery.js';

function input(message='m1',user='u1',tenant='t1'){return {tenant_key:tenant,user_id:user,chat_id:'chat-'+user,source_message_id:message,instruction:'任务 '+message};}
function memory(){return new Store(':memory:');}

test('streamed replies contain public commentary and answer without task chrome or tool status',async()=>{
  const s=memory(),task=s.create(input()).task,calls=[];
  const fake={connected:true,raw:async(method,url,data)=>{calls.push({method,url,data});return url.endsWith('/cards')?{card_id:'card'}:{};},
    reply:async(...args)=>{calls.push({reply:args});return {message_id:'message'};}};
  const delivery=new Delivery(s,fake);
  await delivery.tick();assert.equal(calls.length,0); // No queued status bubble.
  s.transition(task.id,'STARTING',{thread_id:'thread'});
  const publish=async(progress,result='',status)=>{
    if(status)s.transition(task.id,status,{progress,result});else{s.update(task.id,{progress,result});s.enqueue(`view:${task.id}`,task.id,'VIEW',{});}
    await delivery.tick();
  };
  await publish('正在');await publish('正在\n\n正在输出');await publish('正在','正在输出完整回复','COMPLETED');
  const activeCards=calls.filter(c=>c.method==='PUT'&&c.data.card&&JSON.parse(c.data.card.data).config.streaming_mode).map(c=>JSON.parse(c.data.card.data));
  const bodies=[...activeCards.map(c=>c.body.elements[0].content),...calls.filter(c=>c.url?.endsWith('/elements/task_content/content')).map(c=>c.data.content)];
  assert.deepEqual(bodies,['正在','正在\n\n正在输出','正在\n\n正在输出完整回复']);
  assert.match(activeCards[0].header.title.content,/^番薯搬砖中\.\.\.\.\.\. \d+s$/);assert.equal(activeCards[0].header.template,'green');
  const final=calls.filter(c=>c.method==='PUT'&&c.url==='/open-apis/cardkit/v1/cards/card').at(-1);
  const card=JSON.parse(final.data.card.data);assert.equal(card.header,undefined);assert.equal(card.config.streaming_mode,false);
  assert.equal(card.body.elements[0].content,bodies.at(-1));
  const serialized=JSON.stringify(calls);
  for(const excluded of ['耗时','查询：','状态：','**回复**','**进度**'])assert(!serialized.includes(excluded),excluded);
  assert(!serialized.includes(`任务 ${task.id}`));
  await new Promise(resolve=>setImmediate(resolve));await delivery.stop();s.close();
});

test('timer refreshes without new output from the agent, reuses the message, and respects delivery retry backoff',async()=>{
  const s=memory(),task=s.create(input()).task,calls=[];let fail=false,replies=0;
  const fake={connected:true,raw:async(method,url,data)=>{calls.push({method,url,data});if(fail)throw Error('network down');return url.endsWith('/cards')?{card_id:'card'}:{};},reply:async()=>{replies++;return {message_id:'message'};}};
  const delivery=new Delivery(s,fake);
  s.transition(task.id,'STARTING',{started_at:Date.now()-1200});await delivery.tick();
  assert.match(JSON.parse(calls.at(-1).data.card.data).header.title.content,/^番薯搬砖中\.\.\.\.\.\. \d+s$/);
  s.update(task.id,{started_at:Date.now()-4200});await delivery.tick();
  const active=JSON.parse(calls.at(-1).data.card.data);assert.match(active.header.title.content,/^番薯搬砖中\.\.\.\.\.\. [4-9]s$/);assert.equal(active.body.elements[0].content,' ');assert.equal(replies,1);
  fail=true;s.update(task.id,{started_at:Date.now()-8200});await delivery.tick();
  const row=s.db.prepare('SELECT * FROM delivery_outbox WHERE id=?').get(`view:${task.id}`);assert.equal(row.status,'RETRYING');
  s.db.prepare('UPDATE delivery_outbox SET next_attempt_at=? WHERE id=?').run(Date.now()+60000,row.id);
  const count=calls.length;await delivery.tick();assert.equal(calls.length,count);
  fail=false;s.transition(task.id,'COMPLETED',{result:'完成正文'});await delivery.tick();
  const final=JSON.parse(calls.at(-1).data.card.data);assert.equal(final.body.elements[0].content,'完成正文');assert.equal(final.config.streaming_mode,false);
  const after=calls.length;await delivery.tick();assert.equal(calls.length,after);assert.equal(replies,1);
  await delivery.stop();s.close();
});

test('outbox retries delivery without calling the agent and serializes card sequences',async()=>{
  const s=memory(),task=s.create(input()).task;s.transition(task.id,'COMPLETED',{result:'ok'});let fail=true;const sequences=[];
  const fake={connected:true,raw:async(method,url,data)=>{if(fail){fail=false;throw Error('network down');}if(url.endsWith('/cards'))return {card_id:'card'};sequences.push(data.sequence);return {};},reply:async()=>({message_id:'reply'})};
  const delivery=new Delivery(s,fake);await delivery.tick();assert.equal(s.get(task.id).status,'COMPLETED');
  s.db.prepare('UPDATE delivery_outbox SET next_attempt_at=0').run();await delivery.tick();
  assert.equal(s.due().length,0);assert.equal(new Set(sequences).size,sequences.length);assert.deepEqual(sequences,[...sequences].sort((a,b)=>a-b));await delivery.stop();s.close();
});

test('a card update that meets a missing file is retried, not given up: only an upload of a file that is gone fails for good',async()=>{
  const s=new Store(':memory:'),task=s.create(input('gone')).task;s.transition(task.id,'COMPLETED',{result:'完成'});
  const missing=Object.assign(Error('ENOENT: no such file or directory'),{code:'ENOENT'});
  const fake={raw:async()=>{throw missing;},reply:async()=>({message_id:'m'})};
  const delivery=new Delivery(s,fake);await delivery.tick();
  assert.equal(s.db.prepare('SELECT status FROM delivery_outbox WHERE id=?').get(`view:${task.id}`).status,'RETRYING');
  await delivery.stop();s.close();
});

test('deliveries only wait for the client to start, not for the long connection to be up',async()=>{
  const s=new Store(':memory:'),task=s.create(input('gate')).task;s.transition(task.id,'COMPLETED',{result:'完成'});let raw=0;
  const fake={started:false,connected:false,raw:async(m,url)=>{raw++;return url.endsWith('/cards')?{card_id:'c'}:{};},reply:async()=>({message_id:'m'})};
  const delivery=new Delivery(s,fake);await delivery.tick();assert.equal(raw,0);
  fake.started=true;await delivery.tick();assert(raw>0);await delivery.stop();s.close();
});

test('a slow upload does not hold back the card updates of other tasks',async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'lark-slow-')),workspace=path.join(dir,'workspace');await fs.mkdir(workspace);
  const big=path.join(workspace,'big.bin');await fs.writeFile(big,'x');
  const s=memory(),a=s.create(input('a','u1')).task,b=s.create(input('b','u2')).task;
  s.transition(a.id,'COMPLETED',{started_at:a.created_at,workspace_path:workspace,result:`[大文件](<${big}>)`});s.transition(b.id,'RUNNING',{started_at:Date.now(),progress:'还在做'});
  let release;const gate=new Promise(r=>release=r),puts=[];
  const f={connected:true,raw:async(method,url,data)=>{if(method==='PUT')puts.push(url);return url.endsWith('/cards')?{card_id:'card'}:{};},reply:async()=>({message_id:'m'}),upload:async()=>{await gate;return {file_key:'k'};}};
  const delivery=new Delivery(s,f);await delivery.tick(); // cards out, the upload is queued
  const uploading=delivery.tick();await new Promise(r=>setTimeout(r,50)); // the upload is now stuck
  puts.length=0;s.enqueue(`view:${b.id}`,b.id,'VIEW',{});
  const next=delivery.tick();await new Promise(r=>setTimeout(r,200));
  assert(puts.length>0,'the other task\'s card was updated while the upload is still running');
  release();await uploading;await next;await delivery.tick();
  assert.equal(s.db.prepare("SELECT status FROM delivery_outbox WHERE kind='FILE'").get().status,'DELIVERED');
  await delivery.stop();s.close();await fs.rm(dir,{recursive:true});
});

test('rate gate runs one call at a time and limits starts per interval',async()=>{
  const gate=new RateGate(2,150),starts=[];let running=0,overlap=false;
  const job=()=>gate.add(async()=>{starts.push(Date.now());if(++running>1)overlap=true;await new Promise(r=>setTimeout(r,5));running--;});
  const t0=Date.now();await Promise.all([job(),job(),job()]);await gate.onIdle();
  assert(!overlap);assert(starts[2]-starts[0]>=140,'third start waits for the window');assert(starts[1]-t0<100);
  await assert.rejects(gate.add(async()=>{throw Error('x');}),/x/);assert.equal(await gate.add(async()=>7),7);
});
