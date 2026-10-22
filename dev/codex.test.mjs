import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {EventEmitter} from 'node:events';
import {config} from '../dist/config.js';
import {Store} from '../dist/storage/database.js';
import {TaskService} from '../dist/tasks/service.js';
import {Scheduler} from '../dist/tasks/scheduler.js';
import {Turn,plainCommand} from '../dist/codex/turn.js';
import {cardJson,cardElements,renderTask} from '../dist/feishu/cards.js';
import {codexInput} from '../dist/codex/input.js';
import {CodexRunner,codexEnv,codexConfig,threadOptions} from '../dist/codex/runner.js';
import {BOT_DOWN} from '../dist/types.js';

// ---- the events of `codex exec --experimental-json`, as the SDK hands them over
const started=id=>({type:'thread.started',thread_id:id}),turnStarted={type:'turn.started'};
const say=(id,text)=>({type:'item.completed',item:{id,type:'agent_message',text}});
const completed={type:'turn.completed',usage:{input_tokens:1,cached_input_tokens:0,output_tokens:1,reasoning_output_tokens:0}};
const failed=message=>({type:'turn.failed',error:{message}});
const aborted=()=>Object.assign(Error('The operation was aborted'),{name:'AbortError'});
const untilAborted=signal=>new Promise((_,reject)=>signal.aborted?reject(aborted()):signal.addEventListener('abort',()=>reject(aborted()),{once:true}));
const until=async(condition)=>{for(let i=0;i<400;i++){if(condition())return;await new Promise(r=>setTimeout(r,5));}throw Error('condition never became true');};
/** A runner whose turns are whatever `script(job,n)` yields, recording every job it is given. */
function fakeCodex(script){
  const codex=Object.assign(new EventEmitter(),{ready:true,jobs:[]});
  codex.run=async function*(job){codex.jobs.push(job);yield* script(job,codex.jobs.length);};
  return codex;
}
const input=(message='m1',user='u1',instruction='帮我做点事')=>({tenant_key:'t',user_id:user,chat_id:'c',source_message_id:message,instruction});
function setup(script,{downloader}={}){
  const store=new Store(':memory:'),codex=fakeCodex(script),service=new TaskService(store,codex,downloader);service.authorized=()=>true;
  const run=async(message='m1',user='u1',instruction)=>{
    const task=store.create(input(message,user,instruction)).task;store.transition(task.id,'STARTING',{started_at:Date.now()});
    await service.start(store.get(task.id));return store.get(task.id);
  };
  return {store,codex,service,run};
}
const notices=store=>store.db.prepare("SELECT payload FROM delivery_outbox WHERE kind='NOTICE' ORDER BY rowid").all().map(r=>JSON.parse(r.payload).text);

// ---- what a turn looks like on a card
test('while a turn runs every message is progress; when it completes the last one is the answer',()=>{
  const turn=new Turn();
  assert.equal(turn.apply(started('th')),false);assert.equal(turn.threadId,'th');assert.equal(turn.apply(turnStarted),false);
  assert.equal(turn.apply(say('a','先看看文件')),true);assert.equal(turn.apply(say('a','先看看文件')),false,'the same message again changes nothing');
  assert.equal(turn.apply({type:'item.started',item:{id:'c',type:'command_execution',command:'dir',aggregated_output:'',status:'in_progress'}}),true,'a command that starts is a step of its own');
  assert.equal(turn.apply({type:'item.started',item:{id:'c',type:'command_execution',command:'dir',aggregated_output:'',status:'in_progress'}}),false);
  assert.equal(turn.apply({type:'item.completed',item:{id:'r',type:'reasoning',text:'SECRET_REASONING'}}),false);
  turn.apply(say('b','文件在这里'));
  assert.deepEqual([turn.progress,turn.answer,turn.state],['先看看文件\n\n文件在这里','','running']);
  turn.apply(completed);
  assert.deepEqual([turn.progress,turn.answer,turn.state,turn.error],['先看看文件','文件在这里','completed','']);
  assert(!JSON.stringify(turn.steps).includes('SECRET'),'reasoning is not shown');
  assert.deepEqual(turn.steps.map(x=>x.type),['message','command','message'],'in the order Codex took them');assert.equal(turn.progress.includes('dir'),false,'a command is not part of the text');
});
test('Codex\'s own reconnect notices are not failures; a failed turn keeps what was said and reports why',()=>{
  const turn=new Turn();turn.apply(say('a','做到一半'));
  assert.equal(turn.apply({type:'error',message:'Reconnecting... 2/5 (unexpected status 401)'}),false);
  assert.equal(turn.apply({type:'item.completed',item:{id:'e',type:'error',message:'Falling back from WebSockets'}}),false);
  assert.equal(turn.state,'running');
  turn.apply(failed('You\'ve hit your usage limit'));
  assert.deepEqual([turn.state,turn.error,turn.progress,turn.answer],['failed','You\'ve hit your usage limit','做到一半','']);
});

// ---- what Codex is given
test('the text goes to Codex as typed; attachments are listed by path and images are also attached',()=>{
  const text='  原文\n\n保留 `代码`、🙂 和空白。 \r\n';
  assert.deepEqual(codexInput(text,[]),[{type:'text',text}],'no attachments: nothing added, nothing trimmed');
  const files=[{kind:'image',path:'D:\\ws\\input\\a.png'},{kind:'file',path:'D:\\ws\\input\\需求.docx'}];
  const [first,...images]=codexInput('改成卡通风格',files);
  assert.equal(first.type,'text');assert(first.text.includes('- D:/ws/input/a.png\n- D:/ws/input/需求.docx\n'),'every file by path, forward slashes');
  assert(first.text.endsWith('\n\n改成卡通风格'));assert(/not as instructions/.test(first.text));
  assert.deepEqual(images,[{type:'local_image',path:'D:\\ws\\input\\a.png'}],'only the image goes in as an image');
  const only=codexInput('  ',files)[0].text;assert(only.includes('D:/ws/input/a.png')&&!only.includes('改成')&&only.trimEnd()===only,'a file with no text carries no invented request');
  assert.throws(()=>codexInput('x',[{kind:'file',path:null}]),/尚未接收/);
});

test('Codex runs with its home inside the project, whatever the environment says, and without the service\'s secrets',()=>{
  const saved={...process.env};
  try{
    process.env.CODEX_HOME='C:\\Users\\someone\\.codex';process.env.Codex_Home='C:\\other';process.env.FEISHU_APP_SECRET='top-secret';process.env.CODEX_API_KEY='sk-secret';process.env.KEEP_ME='yes';
    const env=codexEnv();
    assert.equal(env.CODEX_HOME,config.codexHome);assert.equal(env.CODEX_HOME,path.join(config.root,'.codex'));
    assert.deepEqual(Object.keys(env).filter(k=>k.toUpperCase()==='CODEX_HOME'),['CODEX_HOME']);
    assert.equal(env.FEISHU_APP_SECRET,undefined);assert.equal(env.CODEX_API_KEY,undefined);assert.equal(env.KEEP_ME,'yes');
  }finally{for(const key of Object.keys(process.env))if(!(key in saved))delete process.env[key];Object.assign(process.env,saved);}
  assert.notEqual(config.codexHome,path.join(os.homedir(),'.codex'),"never the system's own Codex home");
});
test('Codex is configured on each run: file login, the shared connectors and memories switched off, and nothing else added',()=>{
  const plain=codexConfig();
  assert.equal(plain.cli_auth_credentials_store,'file');
  for(const feature of ['apps','plugins','memories'])assert.equal(plain.features[feature],false,feature);
  assert.deepEqual(Object.keys(plain.features).sort(),['apps','memories','plugins'],'no other feature is touched');
  assert.deepEqual(Object.keys(plain).sort(),['cli_auth_credentials_store','features'],'no instructions, no tools, no MCP servers');
  const saved={model:config.CODEX_MODEL,effort:config.CODEX_REASONING_EFFORT};
  try{
    Object.assign(config,{CODEX_MODEL:'',CODEX_REASONING_EFFORT:''});
    assert.deepEqual(threadOptions('D:/w'),{workingDirectory:'D:/w',skipGitRepoCheck:true,sandboxMode:'danger-full-access',approvalPolicy:'never'},'no sandbox, nobody asked');
    Object.assign(config,{CODEX_MODEL:'some-model',CODEX_REASONING_EFFORT:'low'});
    const options=threadOptions('D:/w');assert.equal(options.sandboxMode,'danger-full-access');assert.equal(options.model,'some-model');assert.equal(options.modelReasoningEffort,'low');
  }finally{Object.assign(config,{CODEX_MODEL:saved.model,CODEX_REASONING_EFFORT:saved.effort});}
});
test('a new chat is started or an existing one resumed with the shared environment',async()=>{
  const made=[];
  const factory=options=>{const rec={options,started:[],resumed:[]};made.push(rec);const thread=()=>({runStreamed:async(input,turn)=>({events:(async function*(){yield started('th');})(),input,turn})});
    return {startThread:o=>{rec.started.push(o);return thread();},resumeThread:(id,o)=>{rec.resumed.push([id,o]);return thread();}};};
  const runner=new CodexRunner(factory),controller=new AbortController(),input=[{type:'text',text:'hi'}];
  const drain=async job=>{for await(const _ of runner.run(job));};
  await drain({threadId:null,cwd:'D:/w',input,signal:controller.signal});
  await drain({threadId:'th-1',cwd:'D:/w',input,signal:controller.signal});
  assert.equal(made[0].started.length,1);assert.equal(made[0].resumed.length,0);
  assert.equal(made[1].resumed[0][0],'th-1');assert.equal(made[1].resumed[0][1].workingDirectory,'D:/w');
  assert.equal(made[0].options.env.CODEX_HOME,config.codexHome);assert.deepEqual(made[0].options.config,codexConfig());
});
test('Codex is ready when its binary runs and there is a login or an API key; the real binary is found through the package',async()=>{
  const home=await fs.mkdtemp(path.join(os.tmpdir(),'lark-codex-home-')),saved={home:config.codexHome,key:config.CODEX_API_KEY};
  try{
    config.codexHome=home;config.CODEX_API_KEY='';
    const runner=new CodexRunner(),events=[];runner.on('online',()=>events.push('online'));runner.on('offline',()=>events.push('offline'));
    assert.equal(await runner.probe(),false);assert.match(runner.error,/尚未登录/);assert.match(runner.error,/codex login/);assert.match(runner.version,/^\d+\.\d+\.\d+/,'the binary itself answered');
    assert.deepEqual(await fs.readdir(home).then(x=>x.filter(n=>n==='auth.json')),[],'probing creates no login');
    config.CODEX_API_KEY='sk-test';assert.equal(await runner.probe(),true);assert.equal(runner.error,'');
    config.CODEX_API_KEY='';assert.equal(await runner.probe(),false);assert.deepEqual(events,['online','offline'],'only changes are announced');
  }finally{Object.assign(config,{codexHome:saved.home,CODEX_API_KEY:saved.key});await fs.rm(home,{recursive:true,force:true});}
});

// ---- a task from message to result
test('a task runs in the user\'s workspace as a new chat, is bound to it, and ends with the answer; the next task resumes that chat',async()=>{
  const {store,codex,run}=setup(async function*(job,n){
    yield started(job.threadId??`thread-${n}`);yield turnStarted;yield say('a','我先看一下');yield say('b',`第${n}次的答案`);yield completed;
  });
  const first=await run('m1');
  assert.equal(first.status,'COMPLETED');assert.equal(first.result,'第1次的答案');assert.equal(first.progress,'我先看一下');assert.equal(first.error,'');
  const job=codex.jobs[0],session=store.session(first.session_id);
  assert.equal(job.threadId,null,'a first message starts a chat');assert.deepEqual(job.input,[{type:'text',text:'帮我做点事'}]);
  assert.equal(session.thread_id,'thread-1');assert.equal(first.thread_id,'thread-1');
  assert.equal(job.cwd,session.workspace_path);assert.equal(path.basename(job.cwd),'workspace');assert(job.cwd.startsWith(config.user),'inside user/');
  assert.equal(first.workspace_path,job.cwd,'what Codex makes in its working folder is what is delivered');assert((await fs.stat(first.workspace_path)).isDirectory(),'the folder exists before Codex starts');
  assert(store.submitted(first.id));
  const second=await run('m2');
  assert.equal(codex.jobs[1].threadId,'thread-1','the same chat continues');assert.equal(codex.jobs[1].cwd,job.cwd);
  assert.equal(second.result,'第2次的答案');assert.equal(store.session(second.session_id).thread_id,'thread-1','a resumed chat keeps its binding');
  assert.equal(store.sessions().length,1);store.close();
});

test('what Codex says shows up while the task runs, and the user\'s queue is held until it ends',async()=>{
  let release;const gate=new Promise(r=>release=r);
  const {store,service}=setup(async function*(){yield started('th');yield turnStarted;yield say('a','第一步完成');await gate;yield say('b','最后的答案');yield completed;});
  const task=store.create(input()).task;store.transition(task.id,'STARTING',{started_at:Date.now()});
  const running=service.start(store.get(task.id));
  await until(()=>store.get(task.id).progress==='第一步完成');
  assert.equal(store.get(task.id).status,'RUNNING');assert.equal(store.get(task.id).result,'');assert(store.busy('t','u1'));
  const row=store.db.prepare('SELECT status FROM delivery_outbox WHERE id=?').get(`view:${task.id}`);assert.equal(row.status,'PENDING','the card is refreshed');
  release();await running;assert.equal(store.get(task.id).status,'COMPLETED');assert.equal(store.busy('t','u1'),false);store.close();
});

test('a task ends when Codex reports the result, even if something the turn started keeps the process running; it is then stopped',async()=>{
  let stopped=false;
  const {store,service,run}=setup(async function*(job){
    yield started('th');yield turnStarted;yield say('a','请扫码');yield completed;
    // Like `codex exec` with a browser left waiting for a QR scan: the result is out, but the events only end once the browser does, and
    // stopping the process does not change that (the browser holds its output pipe).
    job.signal.addEventListener('abort',()=>{stopped=true;},{once:true});await new Promise(()=>{});
  });
  service.exitWaitMs=20;
  const task=await run();
  assert.equal(task.status,'COMPLETED');assert.equal(task.result,'请扫码');assert(stopped,'the leftover process is stopped');
  store.close();
});

test('a failed turn shows Codex\'s reason even though the process then exits with an error; any other way a run goes wrong is the one bot-down sentence',async()=>{
  let mode;
  const {store,run}=setup(async function*(job,n){
    yield started(`th-${n}`);
    if(mode==='failed'){yield say('a','做了一半');yield failed('You\'ve hit your usage limit');throw Error('Codex Exec exited with code 1: WARNING: proceeding');}
    if(mode==='auth'){yield failed('unexpected status 401 Unauthorized: Missing bearer or basic authentication in header, url: https://api.openai.com/v1/responses');throw Error('Codex Exec exited with code 1');}
    if(mode==='crash')throw Error('Codex Exec exited with code 1: boom');
    if(mode==='silent')return;
    yield completed;
  });
  mode='failed';let task=await run('m1','u1');assert.equal(task.status,'FAILED');assert.equal(task.error,'You\'ve hit your usage limit');assert.equal(task.progress,'做了一半');
  mode='auth';task=await run('m4','u4');assert.equal(task.status,'FAILED');assert.equal(task.error,BOT_DOWN,'a login Codex no longer accepts is the bot being down, not a message for the user');
  mode='crash';task=await run('m2','u2');assert.equal(task.status,'FAILED');assert.equal(task.error,BOT_DOWN);
  mode='silent';task=await run('m3','u3');assert.equal(task.status,'FAILED');assert.equal(task.error,BOT_DOWN,'a run that ends without a result is not a success');
  store.close();
});

test('a chat Codex no longer has is replaced before anything is sent, once; any other failure is not mistaken for it',async()=>{
  const missing=Error('Codex Exec exited with code 1: Error: thread/resume: no rollout found for thread id th-old (code -32600)');
  let behaviour;
  const {store,codex,run}=setup(async function*(job,n){
    if(job.threadId?.startsWith('th-old')&&behaviour==='missing')throw missing;
    if(job.threadId?.startsWith('th-old')&&behaviour==='late'){yield started(job.threadId);throw missing;}
    yield started(`th-${n}`);yield say('a','好的');yield completed;
  });
  behaviour='ok';const first=await run('m1');const old=store.session(first.session_id);
  store.db.prepare('UPDATE user_sessions SET thread_id=? WHERE id=?').run('th-old',old.id);
  behaviour='missing';const second=await run('m2');
  assert.equal(second.status,'COMPLETED');assert.deepEqual(codex.jobs.slice(1).map(j=>j.threadId),['th-old',null],'resumed, then started anew');
  const now=store.session(second.session_id);assert.notEqual(now.id,old.id);assert(store.session(old.id).retired_at);assert.equal(now.workspace_path,old.workspace_path,'same workspace');
  assert.equal(second.thread_id,now.thread_id);assert(second.thread_id);
  assert.equal(store.db.prepare("SELECT count(*) c FROM task_events WHERE task_id=? AND kind='SESSION_REPLACED'").get(second.id).c,1);
  // Once Codex has said anything the same error is a real failure, and the chat is not swapped under the user.
  store.db.prepare('UPDATE user_sessions SET thread_id=? WHERE id=?').run('th-old-2',now.id);
  behaviour='late';const third=await run('m3');assert.equal(third.status,'FAILED');assert.equal(store.session(now.id).retired_at,null);
  store.close();
});

test('/new while a task runs stops its Codex process, keeps what it said, and starts a new chat; a process that will not stop does not hold the user',async()=>{
  const {store,codex,service}=setup(async function*(job,n){yield started(`th-${n}`);yield say('a','做了一点');await untilAborted(job.signal);});
  const task=store.create(input()).task;store.transition(task.id,'STARTING',{started_at:Date.now()});
  const running=service.start(store.get(task.id));await until(()=>store.get(task.id).progress==='做了一点');
  const message=(text,id,user='u1')=>({tenant:'t',user,chat:'c',message:id,text});
  const old=store.session(store.get(task.id).session_id);
  await service.receive(message('/new','c1'));await running;
  const after=store.get(task.id);assert.equal(after.status,'INTERRUPTED');assert.equal(after.progress,'做了一点');assert.equal(after.cancel_requested,1);
  assert.match(notices(store).at(-1),/新的对话/);assert.equal(store.busy('t','u1'),false,'the next input is accepted');assert(store.session(old.id).retired_at,'and it starts a new chat');
  // A process that ignores the abort: /new ends the task anyway, once it has waited a moment.
  const stuck=setup(async function*(){yield started('th');await new Promise(()=>{});});stuck.service.stopWaitMs=30;stuck.service.exitWaitMs=60;
  const t2=stuck.store.create(input()).task;stuck.store.transition(t2.id,'STARTING',{started_at:Date.now()});const hanging=stuck.service.start(stuck.store.get(t2.id));await until(()=>stuck.store.get(t2.id).status==='RUNNING');
  await stuck.service.receive(message('/new','c1'));assert.equal(stuck.store.get(t2.id).status,'INTERRUPTED');assert.equal(stuck.store.busy('t','u1'),false);
  await hanging;assert.equal(stuck.service.interrupt(stuck.store.get(t2.id)),false,'and its run is over too, not left waiting for events that never come');
  store.close();stuck.store.close();
});

test('a task stopped mid-turn ends even when its events never do (a browser left waiting for a QR scan holds Codex\'s output open): its time limit cannot leave the user blocked',async()=>{
  let killed=false;
  const {store,service}=setup(async function*(job){
    yield started('th');yield say('a','请扫码登录');
    // Killing Codex does not end the events while something the turn left running holds the pipe.
    job.signal.addEventListener('abort',()=>{killed=true;},{once:true});await new Promise(()=>{});
  });
  service.exitWaitMs=40;
  const task=store.create(input()).task;store.transition(task.id,'STARTING',{started_at:Date.now()});
  const running=service.start(store.get(task.id));await until(()=>store.get(task.id).progress==='请扫码登录');
  // What the scheduler does when TASK_TIMEOUT_MS is up.
  store.update(task.id,{cancel_requested:1});assert(service.interrupt(store.get(task.id)));
  await running;
  const after=store.get(task.id);assert.equal(after.status,'INTERRUPTED');assert.equal(after.progress,'请扫码登录');assert(killed);
  assert.equal(store.busy('t','u1'),false,'the next input is accepted');assert.equal(service.interrupt(after),false);
  store.close();
});
test('a task cancelled before Codex was started is cancelled, not interrupted; /new ends a queued one and a leftover nobody runs',async()=>{
  const {store,codex,service}=setup(async function*(){yield started('th');yield completed;});
  const task=store.create(input()).task;store.transition(task.id,'STARTING',{started_at:Date.now()});store.update(task.id,{cancel_requested:1});
  await service.start(store.get(task.id));assert.equal(store.get(task.id).status,'CANCELLED');assert.equal(codex.jobs.length,0);
  const queued=store.create(input('m2','u2')).task,leftover=store.create(input('m3','u3')).task;store.transition(leftover.id,'RUNNING');
  const message=(text,id,user)=>({tenant:'t',user,chat:'c',message:id,text});
  await service.receive(message('/new','x1','u2'));assert.equal(store.get(queued.id).status,'CANCELLED');
  await service.receive(message('/new','x2','u3'));assert.equal(store.get(leftover.id).status,'INTERRUPTED','nothing runs it, so nothing can answer');
  assert(!store.busy('t','u2')&&!store.busy('t','u3'));store.close();
});
test('the bot answers /new and nothing else: other slashes are the user\'s words for Codex',async()=>{
  const {store,codex,service}=setup(async function*(){yield started('th');yield completed;});
  const say_=(text,id,user)=>service.receive({tenant:'t',user,chat:'c',message:id,text});
  for(const [i,text] of ['/help','/status 123','/whoami','/etc/hosts 这是什么'].entries())await say_(text,'s'+i,'u'+i);
  assert.deepEqual(store.list().map(t=>t.instruction),['/help','/status 123','/whoami','/etc/hosts 这是什么'],'each is a task');assert.equal(notices(store).length,0,'and the bot says nothing of its own');
  await say_('/new 帮我做点事','n1','u9');assert.equal(store.list().length,5,'only the exact /new is the command');
  await say_('/new','n2','u10');assert.match(notices(store).at(-1),/还没有对话/);
  store.close();
});
test('the task timeout stops the run, as /new does',async()=>{
  const {store,service}=setup(async function*(job){yield started('th');await untilAborted(job.signal);});
  const saved=config.TASK_TIMEOUT_MS;config.TASK_TIMEOUT_MS=50;
  try{
    service.codex.ready=true;const task=store.create(input()).task;const scheduler=new Scheduler(service);scheduler.tick();
    await until(()=>store.get(task.id).status==='RUNNING');await new Promise(r=>setTimeout(r,80));scheduler.tick();
    await until(()=>store.get(task.id).status==='INTERRUPTED');assert.equal(store.get(task.id).cancel_requested,1);await scheduler.stop();
  }finally{config.TASK_TIMEOUT_MS=saved;store.close();}
});

test('stopping the service ends every Codex process and finishes its task as failed, so no card keeps saying "running"',async()=>{
  const {store,service}=setup(async function*(job,n){yield started(`th-${n}`);yield say('a','进行中');await untilAborted(job.signal);});
  const tasks=['a','b'].map(user=>{const t=store.create(input('m-'+user,user)).task;store.transition(t.id,'STARTING',{started_at:Date.now()});return t;});
  const runs=tasks.map(t=>service.start(store.get(t.id)));await until(()=>tasks.every(t=>store.get(t.id).status==='RUNNING'));
  service.stop();await Promise.all(runs);
  for(const t of tasks){const after=store.get(t.id);assert.equal(after.status,'FAILED');assert.equal(after.error,BOT_DOWN);assert.equal(after.progress,'进行中');}
  assert(tasks.every(t=>store.db.prepare('SELECT status FROM delivery_outbox WHERE id=?').get(`view:${t.id}`).status==='PENDING'),'their cards are updated');
  store.close();
});

test('a restart finishes what was running as failed, cancels what was only queued, and drops what was never delivered',()=>{
  const store=new Store(':memory:'),mk=n=>store.create({tenant_key:'t',user_id:n,chat_id:'c',source_message_id:n,instruction:'x'}).task;
  const queued=mk('q'),starting=mk('s'),running=mk('r'),done=mk('d');
  store.transition(starting.id,'STARTING');store.transition(running.id,'RUNNING',{progress:'做了一半'});store.transition(done.id,'COMPLETED');
  store.enqueue('notice:1',null,'NOTICE',{chat:'c',message:'q',text:'late'});
  const result=store.discardLeftovers();
  assert.deepEqual(result,{cancelled:1,dropped:3,interrupted:2});
  assert.equal(store.get(queued.id).status,'CANCELLED');for(const t of [starting,running]){assert.equal(store.get(t.id).status,'FAILED');assert.equal(store.get(t.id).error,BOT_DOWN);}
  assert.equal(store.get(running.id).progress,'做了一半');
  const status=id=>store.db.prepare('SELECT status FROM delivery_outbox WHERE id=?').get(id).status;
  assert.equal(status('notice:1'),'FAILED');assert.equal(status(`view:${done.id}`),'FAILED');assert.equal(status(`view:${queued.id}`),'FAILED','a cancelled queued input says nothing');
  assert.equal(status(`view:${running.id}`),'PENDING','but the card of a task that was running is finished');assert.equal(status(`view:${starting.id}`),'PENDING');
  assert.deepEqual(store.due().map(r=>r.task_id).sort(),[running.id,starting.id].sort());
  assert.equal(store.active().length,0);store.close();
});

test('/new forgets the conversation: the next task starts a new chat, in the same workspace',async()=>{
  const {store,codex,service,run}=setup(async function*(job,n){yield started(`th-${n}`);yield say('a','ok');yield completed;});
  const message=(text,id)=>({tenant:'t',user:'u1',chat:'c',message:id,text});
  await service.receive(message('/new','n0'));assert.match(notices(store).at(-1),/还没有对话/);
  const first=await run('m1');const old=store.session(first.session_id);
  await service.receive(message('/new','n2'));assert.match(notices(store).at(-1),/新的对话/);assert(store.session(old.id).retired_at);
  const next=await run('m2');assert.equal(codex.jobs.at(-1).threadId,null);assert.notEqual(next.session_id,old.id);assert.equal(store.session(next.session_id).workspace_path,old.workspace_path);
  assert.equal(store.session(old.id).thread_id,'th-1','the old chat is only set aside');store.close();
});

// ---- attachments, images
test('attachments are saved in the workspace before Codex starts; images go in as images, every file by path',async()=>{
  const downloader={downloadResource:async(_message,attachment,file)=>{await fs.writeFile(file,'data');return {size:4,contentType:attachment.kind==='image'?'image/png':'application/pdf'};}};
  const {store,codex,service}=setup(async function*(){yield started('th');yield say('a','看到了');yield completed;},{downloader});
  const task=store.create(input('m1','u1','总结一下'),[],[{kind:'image',key:'i',name:'图.png'},{kind:'file',key:'f',name:'报告.pdf'}]).task;
  store.transition(task.id,'STARTING',{started_at:Date.now()});await service.start(store.get(task.id));
  const done=store.get(task.id);assert.equal(done.status,'COMPLETED');assert.equal(done.progress,'','the "receiving attachments" label does not linger');
  const job=codex.jobs[0],[text,...rest]=job.input,saved=store.inputs(task.id).map(i=>i.path),realWorkspace=await fs.realpath(job.cwd);
  assert(saved.every(file=>path.dirname(file)===path.join(realWorkspace,'input')),"in the workspace's input/ folder");
  assert(saved.every(file=>text.text.includes(file.replaceAll('\\','/'))));assert(text.text.endsWith('\n\n总结一下'));
  assert.deepEqual(rest,[{type:'local_image',path:saved[0]}]);assert.equal(await fs.readFile(saved[1],'utf8'),'data');
  // A download that fails ends the task before Codex hears of it.
  const bad=setup(async function*(){assert.fail('Codex must not start');},{downloader:{downloadResource:async()=>{throw Error('缺少 im:resource 权限');}}});
  const t2=bad.store.create(input('m1','u1'),[],[{kind:'file',key:'f',name:'a.pdf'}]).task;bad.store.transition(t2.id,'STARTING');await bad.service.start(bad.store.get(t2.id));
  assert.equal(bad.store.get(t2.id).status,'FAILED');assert.equal(bad.store.get(t2.id).error,BOT_DOWN);assert.equal(bad.codex.jobs.length,0);assert(!bad.store.submitted(t2.id));
  store.close();bad.store.close();
});
test('the images Codex generated while the task ran are found in its chat\'s folder, and nowhere else',async()=>{
  const home=await fs.mkdtemp(path.join(os.tmpdir(),'lark-gen-home-')),saved=config.codexHome;config.codexHome=home;
  try{
    const png=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=','base64');
    const dir=thread=>path.join(home,'generated_images',thread);await fs.mkdir(dir('th-1'),{recursive:true});await fs.mkdir(dir('th-other'),{recursive:true});
    await fs.writeFile(path.join(dir('th-1'),'before.png'),png);const long=new Date(Date.now()-3600000);await fs.utimes(path.join(dir('th-1'),'before.png'),long,long);
    await fs.writeFile(path.join(dir('th-other'),'foreign.png'),png);
    const {store,run}=setup(async function*(){yield started('th-1');await fs.writeFile(path.join(dir('th-1'),'new-image.png'),png);await fs.writeFile(path.join(dir('th-1'),'notes.txt'),'x');yield say('a','画好了');yield completed;});
    const task=await run('m1');assert.equal(task.status,'COMPLETED');
    assert.deepEqual(store.images(task.id).map(i=>[i.id,path.basename(i.savedPath)]),[['new-image','new-image.png']],'this chat, this task, PNG files only');
    store.close();
  }finally{config.codexHome=saved;await fs.rm(home,{recursive:true,force:true});}
});


// ---- the steps of a turn on a card
test('commands, file changes, tools, searches and plans become steps, the shell wrapper is dropped, and a step is updated in place',()=>{
  const turn=new Turn(),command=(status,output='',exit)=>({type:'item.'+(status==='in_progress'?'started':'completed'),item:{id:'c1',type:'command_execution',command:'"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command "Get-ChildItem -Force"',aggregated_output:output,status,exit_code:exit}});
  turn.apply(command('in_progress'));assert.deepEqual(turn.steps[0],{id:'c1',type:'command',command:'Get-ChildItem -Force',output:'',status:'running'});
  turn.apply(command('completed','a.txt',0));assert.equal(turn.steps.length,1,'the same command, finished');assert.deepEqual([turn.steps[0].status,turn.steps[0].exit,turn.steps[0].output],['done',0,'a.txt']);
  turn.apply({type:'item.completed',item:{id:'c2',type:'command_execution',command:'false',aggregated_output:'',status:'completed',exit_code:1}});assert.equal(turn.steps[1].status,'failed','a non-zero exit is a failure even when Codex ran it');
  turn.apply({type:'item.completed',item:{id:'f',type:'file_change',status:'completed',changes:[{path:'a.txt',kind:'add'}]}});
  turn.apply({type:'item.completed',item:{id:'t',type:'mcp_tool_call',server:'docs',tool:'find',arguments:{q:'x'},status:'completed',result:{content:[{type:'text',text:'found'},{type:'image'}]}}});
  turn.apply({type:'item.completed',item:{id:'w',type:'web_search',query:'番薯'}});turn.apply({type:'item.updated',item:{id:'p',type:'todo_list',items:[{text:'a',completed:false}]}});
  assert.deepEqual(turn.steps.slice(2).map(x=>x.type),['files','tool','search','todo']);assert.equal(turn.steps[3].result,'found\n[image]');
  const long=new Turn();long.apply({type:'item.completed',item:{id:'l',type:'command_execution',command:'x',aggregated_output:'a'.repeat(10000)+'END',status:'completed',exit_code:0}});
  assert(long.steps[0].output.length<3100&&long.steps[0].output.endsWith('END'),'the end of a long output is what is kept');
  assert.equal(plainCommand('git status'),'git status');assert.equal(plainCommand('pwsh -NoProfile -Command "ls"'),'ls');
});
test('the card lists the steps as Codex took them: commands, file changes and tool calls are not shown, long notes fold, the answer stays open, and the whole card fits',()=>{
  const steps=[{id:'1',type:'message',text:'先看一下。'},{id:'2',type:'command',command:'dir',output:'a.txt',status:'done',exit:0},{id:'3',type:'message',text:'长'.repeat(700)},
    {id:'4',type:'files',changes:[{path:'D:/w/a.txt',kind:'add'}],status:'done'},{id:'5',type:'message',text:'这是最终答案'}];
  const task={status:'COMPLETED',progress:'',result:'这是最终答案',error:'',steps:JSON.stringify(steps),workspace_path:'D:/w',started_at:1,created_at:1};
  const tags=cardElements(task,renderTask(task)).map(e=>e.tag+(e.tag==='collapsible_panel'?':'+[...e.header.title.content].slice(0,5).join(''):''));
  assert.deepEqual(tags,['markdown','collapsible_panel:💬 长长长','markdown']);
  const elements=cardElements(task,'');assert.equal(elements.at(-1).content,'这是最终答案');assert(!JSON.stringify(elements).includes('dir')&&!JSON.stringify(elements).includes('a.txt\n'),'the command and its output are not on the card');
  assert(!JSON.stringify(elements).includes('修改了'),'nor the file changes');
  assert.equal(elements.every(e=>e.expanded!==true),true,'folded by default');
  const failed={...task,status:'FAILED',error:'出错了'};assert.equal(cardElements(failed,'').at(-1).content,'出错了\n\n执行失败');
  // A card with no steps (an older task, or one that never started) is the plain text it always was.
  assert.deepEqual(cardElements({...task,steps:'[]'},'正文'),[{tag:'markdown',element_id:'task_content',content:'正文'}]);assert.equal(cardElements({...task,steps:undefined},'x')[0].element_id,'task_content');assert.equal(cardElements({...task,steps:'not json'},'x')[0].element_id,'task_content');
  // The whole card has to fit in what Feishu accepts: the oldest panels give up their contents first, then the oldest steps go.
  const many=Array.from({length:80},(_,i)=>({id:'c'+i,type:'message',text:'n'+i+'x'.repeat(3000)})).concat([{id:'m',type:'message',text:'完成'}]);
  const big=cardJson({...task,steps:JSON.stringify(many)},'完成',false);assert(Buffer.byteLength(JSON.stringify(big))<28000);
  const body=big.body.elements;assert.equal(body.at(-1).content,'完成','the answer is never what gets dropped');assert(JSON.stringify(body).includes('内容已省略')||JSON.stringify(body).includes('已省略'));
  const header=cardJson({...task,status:'RUNNING',started_at:Date.now()-3000},'',true);assert.match(header.header.title.content,/^番薯搬砖中\.\.\.\.\.\. \d+s$/,'the working header stays');
});
test('a running task keeps its steps on the task, and they are what the card is made from',async()=>{
  let release;const gate=new Promise(r=>release=r);
  const {store,service}=setup(async function*(){yield started('th');yield {type:'item.started',item:{id:'c',type:'command_execution',command:'dir',aggregated_output:'',status:'in_progress'}};await gate;
    yield {type:'item.completed',item:{id:'c',type:'command_execution',command:'dir',aggregated_output:'a.txt',status:'completed',exit_code:0}};yield say('m','好了');yield completed;});
  const task=store.create(input()).task;store.transition(task.id,'STARTING',{started_at:Date.now()});const running=service.start(store.get(task.id));
  await until(()=>JSON.parse(store.get(task.id).steps).length===1);
  assert.equal(JSON.parse(store.get(task.id).steps)[0].status,'running');assert.equal(store.get(task.id).progress,'','no text yet, but the card shows the command');
  assert.equal(store.db.prepare('SELECT status FROM delivery_outbox WHERE id=?').get('view:'+task.id).status,'PENDING');
  release();await running;const done=store.get(task.id);assert.deepEqual(JSON.parse(done.steps).map(x=>[x.type,x.status??'']),[['command','done'],['message','']]);assert.equal(done.result,'好了');store.close();
});

test('every turn is handed the user it is for: their own folder (not the workspace in it) and their Open ID',async()=>{
  const {codex,store,run}=setup(async function*(){yield started('th');yield turnStarted;yield say('a','好');yield completed;});
  const task=await run('m1','ou_alice'),job=codex.jobs[0],session=store.session(task.session_id);
  assert.equal(job.user.openId,'ou_alice');
  assert.equal(job.user.dir,path.dirname(session.workspace_path),'user/<name>, the parent of workspace/');
  assert(path.isAbsolute(job.user.dir)&&job.user.dir.startsWith(config.user));
  await run('m2','ou_bob');assert.notEqual(codex.jobs[1].user.dir,job.user.dir,'another user, another folder');assert.equal(codex.jobs[1].user.openId,'ou_bob');
});
