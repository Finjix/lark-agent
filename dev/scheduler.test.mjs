import test from 'node:test';
import assert from 'node:assert/strict';
import {Store} from '../dist/storage/database.js';
import {canSchedule,Scheduler} from '../dist/tasks/scheduler.js';
import {TaskService} from '../dist/tasks/service.js';

function input(message='m1',user='u1',tenant='t1'){return {tenant_key:tenant,user_id:user,chat_id:'chat-'+user,source_message_id:message,instruction:'任务 '+message};}
function memory(){return new Store(':memory:');}

test('session and resource conflicts block a user\'s next task while one is starting or running, without a global task cap',()=>{
  const s=memory(),a=s.create(input('a')).task,b=s.create(input('b')).task,c=s.create(input('c')).task,d=s.create(input('d','u2')).task;
  a.status='STARTING';b.status='RUNNING';
  assert.equal(canSchedule(c,[a,b]),false);assert.equal(canSchedule(d,[a,b]),true);
  c.thread_id='same';d.thread_id='same';assert.equal(canSchedule(c,[d]),false);
  c.thread_id=null;c.resources='["shared"]';d.resources='["shared"]';assert.equal(canSchedule(c,[d]),false);s.close();
});

test('equal-timestamp messages serialize per user and a blocked user does not block other users',async()=>{
  const s=memory(),releases=[];
  const tasks=['A','A','A','B','C'].map((user,i)=>s.create(input('fifo-'+i,user)).task);
  for(const t of tasks)s.update(t.id,{created_at:1000});
  assert.deepEqual(s.list('QUEUED').map(t=>t.id),tasks.map(t=>t.id));
  const service=new TaskService(s,{ready:true});service.start=()=>new Promise(resolve=>releases.push(resolve));
  const scheduler=new Scheduler(service);scheduler.tick();
  assert.deepEqual(s.active().map(t=>t.id),[tasks[0].id,tasks[3].id,tasks[4].id]);
  await new Promise(r=>setImmediate(r));for(const release of releases)release();await scheduler.stop();s.close();
});

test('scheduler launches every independent user without a numeric cap and stop awaits all submissions',async()=>{
  const s=memory(),releases=[],started=[];
  const tasks=Array.from({length:24},(_,i)=>s.create(input(`parallel-${i}`,`user-${i}`)).task);
  const next=s.create(input('same-user-next','user-0')).task;
  const service={store:s,agent:{ready:true},paused:()=>false,start:task=>{started.push(task.id);return new Promise(resolve=>releases.push(resolve));}};
  const scheduler=new Scheduler(service);scheduler.tick();await new Promise(resolve=>setImmediate(resolve));
  assert.equal(started.length,24);assert.equal(s.active().length,24);assert.equal(s.get(next.id).status,'QUEUED');
  scheduler.tick();await new Promise(resolve=>setImmediate(resolve));assert.equal(started.length,24);
  let stopped=false;const stopping=scheduler.stop().then(()=>{stopped=true;});await new Promise(resolve=>setImmediate(resolve));assert.equal(stopped,false);
  for(const release of releases)release();await stopping;assert.equal(stopped,true);
  s.transition(tasks[0].id,'COMPLETED');scheduler.tick();assert.equal(s.get(next.id).status,'QUEUED');s.close();
});
