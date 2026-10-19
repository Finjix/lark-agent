import test from 'node:test';
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {Store} from '../dist/storage/database.js';
import {TaskService,UNAVAILABLE} from '../dist/tasks/service.js';
import {Scheduler,canSchedule} from '../dist/tasks/scheduler.js';
import {clockOffset} from '../dist/feishu/clock.js';

const message=(id,user='u1',extra={})=>({tenant:'t',user,chat:'c',message:id,text:'hi',isGroup:false,...extra});
const notices=store=>store.db.prepare("SELECT payload FROM delivery_outbox WHERE kind='NOTICE' ORDER BY rowid").all().map(r=>JSON.parse(r.payload));
function setup(ready){
  const store=new Store(':memory:'),codex=Object.assign(new EventEmitter(),{ready}),service=new TaskService(store,codex);
  service.authorized=()=>true;return {store,codex,service};
}

test('while Codex is unavailable nothing is queued; the user gets one plain refusal, never a promise to run later',async()=>{
  const {store,codex,service}=setup(false);
  await service.receive(message('m1'));await service.receive(message('m1'));await service.receive(message('m2'));
  assert.equal(store.list().length,0,'no task is created');
  const sent=notices(store);assert.equal(sent.length,2,'one refusal per message, none for a redelivered event');
  assert(sent.every(n=>n.text===UNAVAILABLE));assert(!sent.some(n=>/已收到|恢复后|无需重新发送|前面还有/.test(n.text)));
  codex.ready=true;await service.receive(message('m3'));assert.equal(store.list().length,1,'works again as soon as Codex is back');
  store.close();
});

test('with Codex ready nothing extra is sent, for one user or several',async()=>{
  const {store,service}=setup(true);
  await service.receive(message('a1'));await service.receive(message('b1','u2'));await service.receive(message('c1','u3'));
  assert.equal(store.list().length,3);assert.equal(notices(store).length,0);
  store.close();
});

test('a redelivered event for a task that already exists is not refused while Codex is down',async()=>{
  const {store,codex,service}=setup(true);
  await service.receive(message('m1'));codex.ready=false;await service.receive(message('m1'));
  assert.equal(store.list().length,1);assert.equal(notices(store).length,0);
  store.close();
});

test('messages sent before this service started are ignored, later ones run',async()=>{
  const {store,service}=setup(true),now=Date.now();
  await service.receive(message('old',undefined,{createdAt:now-60000}));
  assert.equal(store.list().length,0);assert.equal(notices(store).length,0,'silent: it is a stale redelivery, not a new request');
  await service.receive(message('fresh',undefined,{createdAt:now}));await service.receive(message('unknown-age','u2'));
  assert.equal(store.list().length,2);
  store.close();
});

test('a task whose Codex process cannot be started fails at once instead of waiting in a queue',async()=>{
  const {store,codex,service}=setup(true);
  codex.run=async function*(){throw Error('spawn codex ENOENT');};
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

test('starting the service voids what the last run left behind: queued tasks, undelivered notices, replies of finished tasks; what was running is over',()=>{
  const store=new Store(':memory:'),mk=n=>store.create({tenant_key:'t',user_id:'u',chat_id:'c',source_message_id:n,instruction:'x'}).task;
  const queued=mk('q'),done=mk('d'),running=mk('r');
  store.transition(done.id,'COMPLETED');store.transition(running.id,'RUNNING');
  store.enqueue('notice:1',null,'NOTICE',{chat:'c',message:'q',text:'late'});
  store.enqueue(`file:${done.id}`,done.id,'FILE',{});store.db.prepare("UPDATE delivery_outbox SET status='RETRYING' WHERE id=?").run(`view:${done.id}`);
  const result=store.discardLeftovers();
  assert.equal(result.cancelled,1);assert.equal(store.get(queued.id).status,'CANCELLED');assert.equal(store.get(running.id).status,'FAILED','its Codex process died with the last run');
  const status=id=>store.db.prepare('SELECT status FROM delivery_outbox WHERE id=?').get(id).status;
  assert.equal(status('notice:1'),'FAILED');assert.equal(status(`file:${done.id}`),'FAILED');assert.equal(status(`view:${done.id}`),'FAILED');
  assert.equal(status(`view:${running.id}`),'PENDING','its card is finished, not left at "running"');
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

test('every unavailable case tells the user the same one sentence, and nothing about Codex internals',async()=>{
  const {BOT_DOWN}=await import('../dist/types.js');
  assert.equal(BOT_DOWN,'机器人罢工了，请联系 @钟丰骏-实习生');assert.equal(UNAVAILABLE,BOT_DOWN);
  const {store,codex,service}=setup(true);
  codex.run=async function*(){throw Error('Codex Exec exited with code 1: 401 Unauthorized');};
  await service.receive(message('m1'));const [task]=store.list();
  store.transition(task.id,'STARTING',{started_at:Date.now()});await service.start(store.get(task.id));
  assert.equal(store.get(task.id).error,BOT_DOWN);assert(!JSON.stringify(store.get(task.id)).includes('Unauthorized'),'the cause is for the log');
  codex.ready=false;await service.receive(message('m2','u2'));assert.equal(notices(store).at(-1).text,BOT_DOWN);
  store.close();
});

test('the bot-down reply is that sentence alone, with no "执行失败" after it',async()=>{
  const {renderTask}=await import('../dist/feishu/cards.js'),{BOT_DOWN}=await import('../dist/types.js');
  assert.equal(renderTask({status:'FAILED',progress:'',result:'',error:BOT_DOWN}),BOT_DOWN);
  assert(renderTask({status:'FAILED',progress:'',result:'',error:'模型出错'}).endsWith('执行失败'),'other failures keep their label');
});

test("stale checks run on Feishu's clock, so a machine whose clock runs fast still hears live messages",async()=>{
  const {store,service}=setup(true),now=Date.now();
  service.clockOffsetMs=-60000; // this machine is a minute ahead of Feishu
  await service.receive(message('live',undefined,{createdAt:now-60500}));assert.equal(store.list().length,1,'sent half a second ago by the Feishu clock');
  await service.receive(message('old','u2',{createdAt:now-120000}));assert.equal(store.list().length,1,'a minute old by the Feishu clock is still stale');
  const offset=await clockOffset(async()=>new Response(null,{headers:{date:new Date(Date.now()+60000).toUTCString()}}));
  assert(Math.abs(offset-60000)<2000,`offset ${offset}`);
  await assert.rejects(clockOffset(async()=>new Response(null)),/时间/);
  store.close();
});

test('a message Feishu delivers long after it was sent is stale even if it was sent just after the service started',async()=>{
  const {store,service}=setup(true),now=Date.now();
  await service.receive(message('late',undefined,{createdAt:now-50000}));
  assert.equal(store.list().length,0);assert.equal(notices(store).length,0);
  await service.receive(message('live',undefined,{createdAt:now-500}));assert.equal(store.list().length,1,'a message sent after the start with a normal delay still runs');
  store.close();
});

test('only one input runs at a time: anything sent while the previous one is unfinished is dropped silently, and works again once it ends',async()=>{
  const {store,service}=setup(true);
  await service.receive(message('first'));assert.equal(store.list().length,1);
  await service.receive(message('second'));await service.receive(message('third','u1',{attachments:[{kind:'file',key:'k',name:'a.pdf'}]}));
  assert.equal(store.list().length,1,'nothing is queued');assert.equal(notices(store).length,0,'and the user hears nothing');
  await service.receive(message('other-user','u2'));assert.equal(store.list().length,2,'another user is not affected');
  store.transition(store.list()[0].id,'RUNNING',{started_at:Date.now()});await service.receive(message('while-running'));assert.equal(store.list().length,2,'still dropped while running');
  store.transition(store.list()[0].id,'COMPLETED');await service.receive(message('after'));assert.equal(store.list().length,3,'accepted again once the result is back');
  await service.receive({...message('cmd'),text:'/new'});assert(notices(store).length>0,'/new works while a task runs');assert.equal(store.busy('t','u1'),false);
  store.close();
});

test('a web address in an image link stays a plain link, so the reply is never an empty "done"',async()=>{
  const {renderTask}=await import('../dist/feishu/cards.js');
  const text=renderTask({status:'COMPLETED',progress:'',result:'![蓝色图片](https://example.com/a.png?x=1&y=2)',error:''});
  assert.equal(text,'[蓝色图片](https://example.com/a.png?x=1&y=2)');
  assert.equal(renderTask({status:'COMPLETED',progress:'',result:'![x](data:image/png;base64,AAAA)',error:''}),'本轮已完成。');
});
