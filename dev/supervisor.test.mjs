import test from 'node:test';
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {Supervisor} from '../dist/supervisor.js';
import {connectionDetail} from '../dist/feishu/client.js';

const wait=ms=>new Promise(r=>setTimeout(r,ms));
const until=async(ok,ms=2000)=>{for(const end=Date.now()+ms;!ok();){if(Date.now()>end)throw Error('timed out');await wait(5);}};
/** Stand-ins for service processes: `health` is what the current one answers (undefined: nothing), `exit` ends it. */
function harness(options={}){
  const children=[],killed=[],shutdowns=[];let health=async()=>({pollAgeMs:0});
  const spawn=()=>{
    const child=Object.assign(new EventEmitter(),{pid:100+children.length,exitCode:null,signalCode:null,
      exit(code,signal=null){if(child.exitCode!==null||child.signalCode!==null)return;child.exitCode=signal?null:code;child.signalCode=signal;child.emit('exit',child.exitCode,signal);}});
    children.push(child);return child;
  };
  const supervisor=new Supervisor({start:spawn,health:()=>children.length?health():Promise.resolve(undefined),
    kill:child=>{killed.push(child.pid);child.exit(null,'SIGKILL');},shutdown:async()=>{shutdowns.push(1);children.at(-1)?.exit(0);},
    checkMs:5,graceMs:30,strikes:3,staleMs:1000,stopWaitMs:50,restartMs:5,maxRestartMs:20,steadyMs:10000,...options});
  return {supervisor,children,killed,shutdowns,answer:fn=>{health=fn;}};
}

test('a service that stops answering /health is killed and started again',async()=>{
  const h=harness();const done=h.supervisor.run();
  await until(()=>h.children.length===1);await wait(20);
  assert.equal(h.killed.length,0,'a healthy service is left alone');
  h.answer(async()=>undefined);
  await until(()=>h.children.length===2);
  assert.deepEqual(h.killed,[100],'the frozen one is killed before the next starts');
  h.answer(async()=>({pollAgeMs:0}));await wait(20);assert.equal(h.children.length,2,'the new one is kept while it answers');
  h.supervisor.stop();assert.equal(await done,0);
});

test('one missed answer is not enough; a check hanging inside the service is',async()=>{
  const h=harness();const done=h.supervisor.run();
  await until(()=>h.children.length===1);
  let calls=0;h.answer(async()=>++calls%3===0?undefined:{pollAgeMs:0});
  await wait(60);assert.equal(h.killed.length,0,'an occasional miss is tolerated');
  h.answer(async()=>({pollAgeMs:5000}));
  await until(()=>h.children.length===2);assert.deepEqual(h.killed,[100]);
  h.supervisor.stop();await done;
});

test('a service still starting is given its grace period before it must answer',async()=>{
  const h=harness({graceMs:200});h.answer(async()=>undefined);const done=h.supervisor.run();
  await until(()=>h.children.length===1);await wait(100);
  assert.equal(h.killed.length,0);
  await until(()=>h.killed.length===1,1000);
  h.supervisor.stop();await done;
});

test('a crash is restarted; a stop with code 0 ends the supervisor too',async()=>{
  const h=harness();const done=h.supervisor.run();
  await until(()=>h.children.length===1);h.children[0].exit(1);
  await until(()=>h.children.length===2);
  h.children[1].exit(0);
  assert.equal(await done,0);assert.equal(h.children.length,2,'not restarted after a deliberate stop');assert.equal(h.killed.length,0);
});

test('stopping the supervisor asks the service to stop safely and does not restart it',async()=>{
  const h=harness();const done=h.supervisor.run();
  await until(()=>h.children.length===1);
  h.supervisor.stop();assert.equal(await done,0);
  assert.equal(h.shutdowns.length,1);assert.equal(h.killed.length,0);assert.equal(h.children.length,1);
});

test('a service that ignores the stop request is killed after the wait',async()=>{
  const h=harness();h.supervisor.options.shutdown=async()=>{};const done=h.supervisor.run();
  await until(()=>h.children.length===1);
  h.supervisor.stop();assert.equal(await done,0);assert.deepEqual(h.killed,[100]);assert.equal(h.children.length,1);
});

test('nothing is started while another service already answers on the port',async()=>{
  const h=harness();h.supervisor.options.health=async()=>({pollAgeMs:0});
  assert.equal(await h.supervisor.run(),1);assert.equal(h.children.length,0);
});

test('restarts after quick failures back off, up to a limit',async()=>{
  const h=harness({restartMs:20,maxRestartMs:80});const done=h.supervisor.run(),starts=[];
  for(let i=0;i<5;i++){await until(()=>h.children.length===i+1);starts.push(Date.now());h.children[i].exit(1);}
  const gaps=starts.slice(1).map((t,i)=>t-starts[i]);
  assert(gaps[1]>=gaps[0]*1.5&&gaps[2]>=70,`gaps grow: ${gaps}`);assert(gaps[3]<200,`but stay capped: ${gaps}`);
  h.supervisor.stop();await done;
});

test('only the long connection\'s SDK logs are kept, without the links they carry',()=>{
  assert.deepEqual(connectionDetail([['[ws]','no pong/inbound within 30s of last ping, terminating to trigger reconnect']]),
    {detail:'no pong/inbound within 30s of last ping, terminating to trigger reconnect'});
  assert.deepEqual(connectionDetail([['ws','unable to connect to the server after trying 3 times")']]),{detail:'unable to connect to the server after trying 3 times")'});
  assert.deepEqual(connectionDetail([['[ws]','get connect config success, ws url: wss://msg-frontier.feishu.cn/ws/v2?ticket=secret']]),
    {detail:'get connect config success, ws url: [URL]'});
  assert.equal(connectionDetail([['[http]','request',{headers:{authorization:'Bearer x'}}]]),undefined);
  assert.equal(connectionDetail([[{url:'x'}]]),undefined);
});
