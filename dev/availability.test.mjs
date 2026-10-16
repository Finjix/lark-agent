import test from 'node:test';
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {Store} from '../dist/storage/database.js';
import {TaskService,UNAVAILABLE} from '../dist/tasks/service.js';
import {Scheduler,canSchedule} from '../dist/tasks/scheduler.js';
import {NotSubmitted} from '../dist/desktop/transport.js';

const message=(id,user='u1',extra={})=>({tenant:'t',user,chat:'c',message:id,text:'hi',isGroup:false,...extra});
const notices=store=>store.db.prepare("SELECT payload FROM delivery_outbox WHERE kind='NOTICE' ORDER BY rowid").all().map(r=>JSON.parse(r.payload));
function setup(ready){
  const store=new Store(':memory:'),desktop=Object.assign(new EventEmitter(),{ready,stream:{states:new Map()}}),service=new TaskService(store,desktop);
  service.authorized=()=>true;return {store,desktop,service};
}

test('while Desktop is unavailable nothing is queued; the user gets one plain refusal, never a promise to run later',async()=>{
  const {store,desktop,service}=setup(false);
  await service.receive(message('m1'));await service.receive(message('m1'));await service.receive(message('m2'));
  assert.equal(store.list().length,0,'no task is created');
  const sent=notices(store);assert.equal(sent.length,2,'one refusal per message, none for a redelivered event');
  assert(sent.every(n=>n.text===UNAVAILABLE));assert(!sent.some(n=>/已收到|恢复后|无需重新发送|前面还有/.test(n.text)));
  desktop.ready=true;await service.receive(message('m3'));assert.equal(store.list().length,1,'works again as soon as Desktop is back');
  store.close();
});

test('with Desktop ready nothing extra is sent, even for several messages from one user',async()=>{
  const {store,service}=setup(true);
  await service.receive(message('a1'));await service.receive(message('a2'));await service.receive(message('a3'));await service.receive(message('b1','u2'));
  assert.equal(store.list().length,4);assert.equal(notices(store).length,0);
  store.close();
});

test('a redelivered event for a task that already exists is not refused while Desktop is down',async()=>{
  const {store,desktop,service}=setup(true);
  await service.receive(message('m1'));desktop.ready=false;await service.receive(message('m1'));
  assert.equal(store.list().length,1);assert.equal(notices(store).length,0);
  store.close();
});

test('messages sent before this service started are ignored, later ones run',async()=>{
  const {store,service}=setup(true),now=Date.now();
  await service.receive(message('old',undefined,{createdAt:now-60000}));
  assert.equal(store.list().length,0);assert.equal(notices(store).length,0,'silent: it is a stale redelivery, not a new request');
  await service.receive(message('fresh',undefined,{createdAt:now}));await service.receive(message('unknown-age'));
  assert.equal(store.list().length,2);
  store.close();
});

test('a task that Desktop never received fails at once instead of waiting in a queue',async()=>{
  const {store,desktop,service}=setup(true);
  Object.assign(desktop,{startTask:async()=>{throw new NotSubmitted('Desktop 尚未就绪');},follow:async()=>{}});
  await service.receive(message('m1'));const [task]=store.list();
  store.transition(task.id,'STARTING',{started_at:Date.now()});
  await service.start(store.get(task.id));
  const after=store.get(task.id);assert.equal(after.status,'FAILED');assert.equal(after.error,UNAVAILABLE);
  store.close();
});

test('there is no global concurrency cap: different users start together, one user\'s messages share one chat and stay in order',()=>{
  const store=new Store(':memory:'),mk=(user,n)=>store.create({tenant_key:'t',user_id:user,chat_id:'c',source_message_id:`${user}-${n}`,instruction:'x'}).task;
  const active=[];
  for(let i=0;i<50;i++){const task=mk(`user-${i}`,0);assert(canSchedule(task,active),'every other user can start');active.push({...task,status:'RUNNING'});}
  assert.equal(canSchedule(mk('user-0',1),active),false,'the same user waits for the previous message in the same chat');
  store.close();
});
