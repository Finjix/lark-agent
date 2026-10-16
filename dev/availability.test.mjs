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

test('starting the service voids what the last run left behind: queued tasks, undelivered notices, replies of finished tasks',()=>{
  const store=new Store(':memory:'),mk=n=>store.create({tenant_key:'t',user_id:'u',chat_id:'c',source_message_id:n,instruction:'x'}).task;
  const queued=mk('q'),done=mk('d'),running=mk('r');
  store.transition(done.id,'COMPLETED');store.transition(running.id,'RUNNING');
  store.enqueue('notice:1',null,'NOTICE',{chat:'c',message:'q',text:'late'});
  store.enqueue(`file:${done.id}`,done.id,'FILE',{});store.db.prepare("UPDATE delivery_outbox SET status='RETRYING' WHERE id=?").run(`view:${done.id}`);
  const result=store.discardLeftovers();
  assert.equal(result.cancelled,1);assert.equal(store.get(queued.id).status,'CANCELLED');assert.equal(store.get(running.id).status,'RUNNING');
  const status=id=>store.db.prepare('SELECT status FROM delivery_outbox WHERE id=?').get(id).status;
  assert.equal(status('notice:1'),'FAILED');assert.equal(status(`file:${done.id}`),'FAILED');assert.equal(status(`view:${done.id}`),'FAILED');
  assert.equal(status(`view:${running.id}`),'PENDING','a task that is still running keeps reporting');
  assert.equal(store.due().filter(r=>r.kind!=='VIEW'||r.task_id===running.id).length,store.due().length);
  assert(!store.due().some(r=>r.id==='notice:1'||r.task_id===done.id||r.task_id===queued.id),'nothing from before the start is sent');
  store.close();
});

test('markdown images never reach a card: Feishu rejects the whole card for a data: URI or a local path',async()=>{
  const {renderTask}=await import('../dist/feishu/cards.js');
  const task={status:'COMPLETED',progress:'我会生成图片。',result:'![绿色](data:image/png;base64,)\n\n![x](<C:/a b/x.png>) 完成 [文件](C:/a/f.txt)',error:''};
  const text=renderTask(task);
  assert(!/!\[/.test(text)&&!text.includes('data:image'));assert(text.includes('我会生成图片。')&&text.includes('完成')&&text.includes('[文件](C:/a/f.txt)'),'text and ordinary links stay');
  assert.equal(renderTask({status:'COMPLETED',progress:'',result:'![a](data:image/png;base64,AAAA)',error:''}),'本轮已完成。');
});

test('every unavailable case tells the user the same one sentence, and nothing about Desktop internals',async()=>{
  const {BOT_DOWN}=await import('../dist/types.js');
  assert.equal(BOT_DOWN,'机器人罢工了，请联系 @钟丰骏-实习生');assert.equal(UNAVAILABLE,BOT_DOWN);
  const {store,desktop,service}=setup(true);
  Object.assign(desktop,{startTask:async()=>{throw new NotSubmitted('Desktop 尚未就绪');},follow:async()=>{}});
  await service.receive(message('m1'));const [task]=store.list();
  store.transition(task.id,'STARTING',{started_at:Date.now()});await service.start(store.get(task.id));
  assert.equal(store.get(task.id).error,BOT_DOWN);
  desktop.reconcileFail=true;Object.assign(desktop,{follow:async()=>{throw Error('boom');}});
  const t2=store.create({tenant_key:'t',user_id:'u9',chat_id:'c',source_message_id:'x',instruction:'x'}).task;
  store.transition(t2.id,'STARTING',{thread_id:'th'});await service.reconcile();
  assert.equal(store.get(t2.id).status,'UNKNOWN');assert.equal(store.get(t2.id).error,BOT_DOWN);
  store.close();
});
