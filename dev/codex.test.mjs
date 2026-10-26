import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import fsSync from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {EventEmitter} from 'node:events';
import {PassThrough} from 'node:stream';
import {config} from '../dist/config.js';
import {Store} from '../dist/storage/database.js';
import {TaskService} from '../dist/tasks/service.js';
import {Scheduler} from '../dist/tasks/scheduler.js';
import {Turn,plainCommand} from '../dist/codex/turn.js';
import {cardJson,cardElements,renderTask} from '../dist/feishu/cards.js';
import {codexInput} from '../dist/codex/input.js';
import {CodexRunner,codexEnv,codexConfig,threadOptions,configArgs,agentInstructions} from '../dist/codex/runner.js';
import {AppServerError,withPath,serverOver} from '../dist/codex/appserver.js';
import {BOT_DOWN,InputRejected} from '../dist/types.js';

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
test('when the user added something after Codex\'s last work and Codex answered it too, the answer is every message since then',()=>{
  const turn=new Turn(),command={type:'item.completed',item:{id:'c',type:'command_execution',command:'dir',aggregated_output:'',status:'completed',exit_code:0}};
  turn.apply(say('m1','先看看'));turn.apply(command);turn.steered('steer:m2','再加个封面',[]);turn.apply(say('m2','报告做好了：report.docx'));turn.apply(say('m3','封面也加好了。'));
  turn.apply({type:'item.updated',item:{id:'p',type:'todo_list',items:[{text:'封面',completed:true}]}});turn.apply(completed);
  assert.deepEqual([turn.answer,turn.progress],['报告做好了：report.docx\n\n封面也加好了。','先看看'],'the first request\'s answer is not pushed into the progress');
  const worked=new Turn();worked.steered('steer:m2','再加个封面',[]);worked.apply(command);worked.apply(say('m1','中间说明'));worked.apply({...command,item:{...command.item,id:'c2'}});worked.apply(say('m2','两件都做好了'));worked.apply(completed);
  assert.deepEqual([worked.answer,worked.progress],['两件都做好了','中间说明'],'taken in while Codex worked: the last message answers, as without it');
});
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
test('Codex is configured on each run: the shared connectors and memories switched off, and nothing else added',()=>{
  const plain=codexConfig();
  for(const feature of ['apps','plugins','memories'])assert.equal(plain.features[feature],false,feature);
  assert.deepEqual(Object.keys(plain.features).sort(),['apps','memories','plugins'],'no other feature is touched');
  assert.deepEqual(Object.keys(plain).sort(),['features'],'no login, model or instructions, no tools, no MCP servers');
  assert.deepEqual(threadOptions('D:/w'),{workingDirectory:'D:/w',skipGitRepoCheck:true,sandboxMode:'danger-full-access',approvalPolicy:'never'},'no sandbox, nobody asked, the model left to .codex/config.toml');
});
/**
 * Stand-ins for `codex app-server` processes, one per turn: each answers what a turn asks (`answers` overrides a method; a function gets the
 * params and the server), records the requests, and hands out what the test `send`s until it `end`s.
 */
function fakeServers(answers={}){
  const made=[];
  const connect=options=>{
    const queue=[],waiting=[];
    const server={options,requests:[],notified:[],closed:false,exitReason:undefined,
      send(method,params){const note={method,params},reader=waiting.shift();if(reader)reader({done:false,value:note});else queue.push(note);},
      end(){server.ended=true;for(const reader of waiting.splice(0))reader({done:true,value:undefined});},
      async request(method,params){
        server.requests.push([method,params]);const answer=answers[method];
        if(typeof answer==='function')return answer(params,server);if(answer instanceof Error)throw answer;
        return method==='thread/start'?{thread:{id:'th-new'}}:method==='thread/resume'?{thread:{id:params.threadId}}:method==='turn/start'?{turn:{id:'turn-1',status:'inProgress'}}
          :method==='turn/steer'?{turnId:params.expectedTurnId}:{};
      },
      notify(method){server.notified.push(method);},
      close(){server.closed=true;},
      notifications:{next:()=>queue.length?Promise.resolve({done:false,value:queue.shift()}):server.ended?Promise.resolve({done:true,value:undefined}):new Promise(r=>waiting.push(r))},
    };
    made.push(server);return server;
  };
  return {connect,made};
}
/** A turn/start that starts the turn and then reports `notes` (method, params) for it. */
const reporting=(...notes)=>(params,server)=>{for(const [method,extra] of notes)server.send(method,{threadId:params.threadId,turnId:'turn-1',...extra});return {turn:{id:'turn-1',status:'inProgress'}};};
const finished=(status='completed',error=null)=>['turn/completed',{turn:{id:'turn-1',status,error}}];
const runAll=async(runner,job)=>{const events=[];for await(const event of runner.run({cwd:'D:/w',input:[],signal:new AbortController().signal,threadId:null,...job}))events.push(event);return events;};

test('a turn runs in an app-server of its own: a new chat is started or an existing one resumed, with the project\'s home and settings, the input as given',async()=>{
  const {connect,made}=fakeServers({'turn/start':reporting(finished())}),runner=new CodexRunner(connect);
  {
    const first=await runAll(runner,{input:[{type:'text',text:'hi'},{type:'local_image',path:'D:/w/input/a.png'}]});
    await runAll(runner,{threadId:'th-1',input:[{type:'text',text:'again'}]});
    assert.deepEqual(first.map(e=>e.type),['thread.started','turn.started','turn.completed']);assert.equal(first[0].thread_id,'th-new');
    const [a,b]=made;
    assert.deepEqual(a.requests.map(r=>r[0]),['initialize','thread/start','turn/start']);assert.deepEqual(a.notified,['initialized']);assert.equal(a.requests[0][1].clientInfo.name,'lark-agent');
    assert.deepEqual(a.requests[1][1],{cwd:'D:/w',sandbox:'danger-full-access',approvalPolicy:'never',developerInstructions:agentInstructions()},'no sandbox, nobody asked, held to the plugins');
    assert.deepEqual(a.requests[2][1],{threadId:'th-new',input:[{type:'text',text:'hi'},{type:'localImage',path:'D:/w/input/a.png'}]});
    assert.deepEqual(b.requests[1],['thread/resume',{threadId:'th-1',cwd:'D:/w',sandbox:'danger-full-access',approvalPolicy:'never',developerInstructions:agentInstructions(),excludeTurns:true}],
      'a resumed chat is not sent back whole, and is held to the plugins as a new one is');
    assert.equal(a.options.env.CODEX_HOME,config.codexHome);
    assert.deepEqual(a.options.args,configArgs(codexConfig()));
    assert(a.closed&&b.closed,'the process of a turn is closed once the turn is over');
  }
});
test('Codex is told to work only through the installed plugins and to do no more on the machine than using them takes',()=>{
  const none=agentInstructions(),some=agentInstructions([{name:'lark-docs'},{name:'lark-sheets'}]);
  assert.match(none,/没有安装任何插件/);assert.doesNotMatch(none,/已安装的插件/);
  assert.match(some,/已安装的插件：lark-docs、lark-sheets。/);assert.doesNotMatch(some,/没有安装任何插件/);
  for(const text of [none,some]){assert.match(text,/只用插件的技能和工具/);assert.match(text,/必不可少/);assert.match(text,/环境变量、密钥、凭证/);}
});
test('config overrides reach the app-server flattened as the SDK flattened them for codex exec',()=>{
  assert.deepEqual(configArgs({cli_auth_credentials_store:'file',features:{apps:false,plugins:true},plugins:{'"x@lark-agent"':{mcp_servers:{'"s"':{enabled:false}}}},
    mcp_servers:{'"s"':{command:'node',args:['a b','c'],env:{'"K"':'v'},cwd:undefined}},n:3,empty:{},table:[{'a b':1}]}),
  ['--config','cli_auth_credentials_store="file"','--config','features.apps=false','--config','features.plugins=true',
    '--config','plugins."x@lark-agent".mcp_servers."s".enabled=false','--config','mcp_servers."s".command="node"','--config','mcp_servers."s".args=["a b", "c"]',
    '--config','mcp_servers."s".env."K"="v"','--config','n=3','--config','empty={}','--config','table=[{"a b" = 1}]']);
  assert.deepEqual(withPath({Path:'C:\\x',PATH:'C:\\y',other:'1'},['C:\\codex'],'win32'),{Path:'C:\\codex;C:\\x',other:'1'},'one spelling of PATH on Windows, the tools in front');
});
test('what the app-server reports becomes the steps a card is made of, as codex exec reported them',async()=>{
  const {connect}=fakeServers({'turn/start':reporting(
    ['item/started',{item:{type:'userMessage',id:'u',content:[{type:'text',text:'原话'}]}}],
    ['item/completed',{item:{type:'agentMessage',id:'a',text:'先看看',phase:'commentary'}}],
    ['item/started',{item:{type:'commandExecution',id:'c',command:'"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command \'dir\'',status:'inProgress',aggregatedOutput:null,exitCode:null}}],
    ['item/commandExecution/outputDelta',{itemId:'c',delta:'a.txt'}],
    ['item/completed',{item:{type:'commandExecution',id:'c',command:'"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command \'dir\'',status:'completed',aggregatedOutput:'a.txt\r\n',exitCode:0}}],
    ['item/completed',{item:{type:'fileChange',id:'f',status:'completed',changes:[{path:'a.txt',kind:{type:'update',move_path:null},diff:'+x'}]}}],
    ['item/completed',{item:{type:'mcpToolCall',id:'m',server:'docs',tool:'find',arguments:{q:1},status:'failed',error:{message:'没找到'}}}],
    ['item/completed',{item:{type:'webSearch',id:'w',query:'番薯'}}],
    ['turn/plan/updated',{plan:[{step:'读文档',status:'completed'},{step:'上传',status:'inProgress'}]}],
    ['turn/plan/updated',{plan:[{step:'读文档',status:'completed'},{step:'上传',status:'completed'}]}],
    ['item/completed',{turnId:'another-turn',item:{type:'agentMessage',id:'x',text:'别的轮次'}}],
    ['error',{error:{message:'Reconnecting... 1/5'},willRetry:true}],
    ['item/completed',{item:{type:'agentMessage',id:'b',text:'完成',phase:'final_answer'}}],
    finished())});
  const turn=new Turn();for(const event of await runAll(new CodexRunner(connect),{}))turn.apply(event);
  assert.deepEqual([turn.state,turn.answer,turn.progress],['completed','完成','先看看']);
  assert.deepEqual(turn.steps.map(s=>s.type),['message','command','files','tool','search','todo','message'],'the user\'s own message is no step');
  const of=Object.fromEntries(turn.steps.map(s=>[s.type,s]));
  assert.deepEqual([of.command.command,of.command.status,of.command.output,of.command.exit],['dir','done','a.txt\r\n',0]);
  assert.deepEqual(of.files.changes,[{path:'a.txt',kind:'update'}]);assert.deepEqual([of.tool.status,of.tool.result],['failed','没找到']);
  assert.deepEqual(of.todo.items,[{text:'读文档',completed:true},{text:'上传',completed:true}],'the plan is one to-do list, updated in place');
  assert(!JSON.stringify(turn.steps).includes('别的轮次'),'only this turn\'s reports count');
});
test('a failed turn says why, a stopped one ends without a result, and a process that ends early or is stopped is an error',async()=>{
  const failedTurn=await runAll(new CodexRunner(fakeServers({'turn/start':reporting(finished('failed',{message:'You\'ve hit your usage limit'}))}).connect),{});
  assert.deepEqual(failedTurn.at(-1),{type:'turn.failed',error:{message:'You\'ve hit your usage limit'}});
  const interrupted=await runAll(new CodexRunner(fakeServers({'turn/start':reporting(finished('interrupted'))}).connect),{});
  assert.deepEqual(interrupted.map(e=>e.type),['thread.started','turn.started'],'no result of its own: the stop says what happened');
  const early=fakeServers({'turn/start':(params,server)=>{server.exitReason='Codex app-server 退出了（code 1）：boom';server.end();return {turn:{id:'turn-1'}};}});
  await assert.rejects(runAll(new CodexRunner(early.connect),{}),/boom/);assert(early.made[0].closed);
  const controller=new AbortController(),stopped=fakeServers({'turn/start':(params,server)=>{controller.abort();server.end();return {turn:{id:'turn-1'}};}});
  await assert.rejects(runAll(new CodexRunner(stopped.connect),{signal:controller.signal}),{name:'AbortError'});
  const missing=fakeServers({'thread/resume':new AppServerError('no rollout found for thread id th-old',-32600)});
  await assert.rejects(runAll(new CodexRunner(missing.connect),{threadId:'th-old'}),/no rollout found/,'which the service takes for a chat Codex no longer has');
});
test('while its turn runs, a turn takes more input: what hands it over is offered once the turn has started and withdrawn when it is over',async()=>{
  let steer;const offers=[];
  const {connect,made}=fakeServers({'turn/start':(params,server)=>({turn:{id:'turn-1',status:'inProgress'}}),
    'turn/steer':(params,server)=>{if(server.ended)throw new AppServerError('no active turn to steer',-32600);server.send('turn/completed',{threadId:params.threadId,turn:{id:'turn-1',status:'completed'}});return {turnId:'turn-1'};}});
  const running=runAll(new CodexRunner(connect),{steerable:fn=>{offers.push(!!fn);if(fn)steer=fn;}});
  await until(()=>steer);assert.deepEqual(offers,[true]);
  await steer([{type:'text',text:'再加一件'},{type:'local_image',path:'D:/w/input/b.png'}]);
  await running;
  assert.deepEqual(made[0].requests.at(-1),['turn/steer',{threadId:'th-new',expectedTurnId:'turn-1',input:[{type:'text',text:'再加一件'},{type:'localImage',path:'D:/w/input/b.png'}]}]);
  assert.deepEqual(offers,[true,false],'withdrawn once the turn is over');
  made[0].end();await assert.rejects(steer([{type:'text',text:'太晚了'}]),/no active turn/);
});
/** A codex.exe process that never answers: its pipes, and what the test makes it do. */
function silentProcess(){
  return Object.assign(new EventEmitter(),{stdin:new PassThrough(),stdout:new PassThrough(),stderr:new PassThrough(),exitCode:null,signalCode:null,killed:false,
    kill(){this.killed=true;return true;}});
}
test('the app-server connection fails what still waits once it is closed, and ends soon after Codex exits even while a leftover process holds its output',async()=>{
  const closing=silentProcess(),silent=serverOver(closing),waiting=silent.request('turn/steer',{});silent.close();
  await assert.rejects(waiting,/已关闭/,'a steer sent as the turn ended fails at once');await assert.rejects(silent.request('turn/steer',{}),/已关闭/,'and so does one sent later');
  assert(closing.killed);assert.deepEqual(await silent.notifications.next(),{done:true,value:undefined});
  // Codex exits, but a process one of its commands started (a browser waiting for a QR scan, say) keeps its output open.
  const leaving=silentProcess(),gone=serverOver(leaving),pending=gone.request('turn/steer',{}),since=Date.now();
  leaving.exitCode=3;leaving.emit('exit',3,null);
  await assert.rejects(pending,/退出了（code 3）/);
  assert(Date.now()-since<3000,'not held until the leftover process ends');assert.deepEqual(await gone.notifications.next(),{done:true,value:undefined});
});
test('Codex is ready when its binary runs, without any login; the real binary is found through the package',async()=>{
  const home=await fs.mkdtemp(path.join(os.tmpdir(),'lark-codex-home-')),saved={home:config.codexHome};
  try{
    config.codexHome=home;
    const runner=new CodexRunner(),events=[];runner.on('online',()=>events.push('online'));
    assert.equal(await runner.probe(),true);assert.equal(runner.error,'');assert.match(runner.version,/^\d+\.\d+\.\d+/,'the binary itself answered');
    assert.deepEqual(await fs.readdir(home).then(x=>x.filter(n=>n==='auth.json')),[],'probing creates no login');
    assert.equal(await runner.probe(),true);assert.deepEqual(events,['online'],'only changes are announced');
  }finally{Object.assign(config,{codexHome:saved.home});await fs.rm(home,{recursive:true,force:true});}
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
  assert.equal(plainCommand('"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command \'Start-Sleep -Seconds 15 ; Write-Output slept\''),'Start-Sleep -Seconds 15 ; Write-Output slept','as the app-server quotes it');
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

// ---- a message sent while the previous one is still unfinished goes into its turn (steering)
const sent=(id,text,extra={})=>({tenant:'t',user:'u1',chat:'c',message:id,text,...extra});
/** Starts the user's first task (m1) and waits until its turn is under way. */
async function running(kit,text='先做这个'){
  const task=kit.store.create(input('m1','u1',text)).task;kit.store.transition(task.id,'STARTING',{started_at:Date.now()});
  const done=kit.service.start(kit.store.get(task.id));await until(()=>kit.store.get(task.id).status==='RUNNING');
  return {task,done};
}
test('a message sent while a turn runs goes into that turn: the task answers both, the message shows on its card and gets no card of its own',async()=>{
  const handed=[];
  const kit=setup(async function*(job){
    yield started('th');yield turnStarted;job.steerable(async input=>{handed.push(input);});
    yield {type:'item.completed',item:{id:'a',type:'agent_message',text:'先做第一件',phase:'commentary'}};await until(()=>handed.length);
    yield say('b',`都做好了，也${handed[0][0].text}`);yield completed;
  });
  const {task,done}=await running(kit);
  await kit.service.receive(sent('m2','加一张封面'));await done;
  const joined=kit.store.taskForMessage('t','m2'),after=kit.store.get(task.id),steps=JSON.parse(after.steps);
  assert.deepEqual([joined.status,after.status,after.result],['STEERED','COMPLETED','都做好了，也加一张封面']);
  assert.equal(kit.codex.jobs.length,1,'no turn of its own');assert.deepEqual(handed,[[{type:'text',text:'加一张封面'}]],'given to the turn as typed');
  assert.deepEqual(steps.map(s=>s.type),['message','user','message'],'shown where it came in');
  assert.deepEqual(steps[1],{id:`steer:${joined.id}`,type:'user',text:'加一张封面',files:[]});
  assert(cardElements(after,renderTask(after)).some(e=>e.content==='➕ 补充：加一张封面'));
  assert.equal(renderTask(joined),'','nothing of its own to show');
  assert.equal(kit.store.db.prepare("SELECT count(*) c FROM task_events WHERE task_id=? AND kind='STEERED'").get(task.id).c,1);
  kit.store.close();
});
test('a message the turn no longer takes (it has just ended) goes back in the queue and runs on its own, in the same chat',async()=>{
  const kit=setup(async function*(job,n){
    yield started('th');yield turnStarted;job.steerable(async()=>{throw new AppServerError('no active turn to steer',-32600);});
    if(n===1){yield say('a','第一件');await until(()=>kit.store.taskForMessage('t','m2')?.status==='QUEUED');}
    yield say('b',`答：${job.input[0].text}`);yield completed;
  });
  const {done}=await running(kit);
  await kit.service.receive(sent('m2','第二件'));await done;
  assert.equal(kit.store.taskForMessage('t','m2').status,'QUEUED');
  const scheduler=new Scheduler(kit.service);scheduler.tick();await until(()=>kit.store.taskForMessage('t','m2').status==='COMPLETED');await scheduler.stop();
  assert.equal(kit.store.taskForMessage('t','m2').result,'答：第二件');assert.equal(kit.codex.jobs.length,2);assert.equal(kit.codex.jobs[1].threadId,'th');
  kit.store.close();
});
test('a message sent before the previous one has started waits for its turn and goes into it; messages go in the order they came, each once',async()=>{
  const handed=[];
  const kit=setup(async function*(job){
    yield started('th');yield turnStarted;job.steerable(async input=>{handed.push(input[0].text);});
    await until(()=>handed.length===2);yield say('a',handed.join('+'));yield completed;
  });
  const first=kit.store.create(input('m1','u1','一')).task;
  await kit.service.receive(sent('m2','二'));await kit.service.receive(sent('m3','三'));await kit.service.receive(sent('m2','二'));
  assert.deepEqual(['m2','m3'].map(m=>kit.store.taskForMessage('t',m).status),['STEERING','STEERING'],'waiting, not queued: the scheduler leaves them alone');
  assert.equal(kit.store.list().length,3,'a redelivered event is no new message');
  kit.store.transition(first.id,'STARTING',{started_at:Date.now()});await kit.service.start(kit.store.get(first.id));
  await until(()=>kit.store.taskForMessage('t','m3').status==='STEERED');
  assert.deepEqual(handed,['二','三']);assert.equal(kit.store.get(first.id).result,'二+三');assert.equal(kit.codex.jobs.length,1);
  kit.store.close();
});
test('the files of a message sent meanwhile arrive in the workspace the turn works in, and go to the turn with it',async()=>{
  const downloader={downloadResource:async(_message,attachment,file)=>{await fs.writeFile(file,'data');return {size:4,contentType:attachment.kind==='image'?'image/png':'application/zip'};}};
  const handed=[];
  const kit=setup(async function*(job){yield started('th');yield turnStarted;job.steerable(async input=>{handed.push(input);});await until(()=>handed.length);yield say('a','收到');yield completed;},{downloader});
  const {task,done}=await running(kit);
  await kit.service.receive(sent('m2','',{attachments:[{kind:'file',key:'z',name:'玩法AI迭代素材.zip'},{kind:'image',key:'i',name:'封面.png'}]}));await done;
  const joined=kit.store.taskForMessage('t','m2'),files=kit.store.inputs(joined.id).map(i=>i.path),[text,...images]=handed[0];
  assert.equal(joined.status,'STEERED');assert(files.every(f=>path.dirname(f)===path.join(fsSync.realpathSync(kit.store.get(task.id).workspace_path),'input')),"in the workspace's input/");
  assert(files.every(f=>text.text.includes(f.replaceAll('\\','/'))),'listed by path, as for a message of its own');assert.deepEqual(images,[{type:'local_image',path:files[1]}]);
  assert.deepEqual(JSON.parse(kit.store.get(task.id).steps).find(s=>s.type==='user'),{id:`steer:${joined.id}`,type:'user',text:'',files:['玩法AI迭代素材.zip','封面.png']});
  assert(cardElements(kit.store.get(task.id),' ').some(e=>e.content==='➕ 补充：（附件）\n📎 玩法AI迭代素材.zip、封面.png'));
  kit.store.close();
});
test('/new stops the running task and cancels what was waiting to go into it; a task that ends before its turn starts sends its waiting messages back to the queue',async()=>{
  const kit=setup(async function*(job){yield started('th');yield turnStarted;await untilAborted(job.signal);});
  const {task,done}=await running(kit);
  await kit.service.receive(sent('m2','第二件'));assert.equal(kit.store.taskForMessage('t','m2').status,'STEERING','the turn offers no way in yet');
  await kit.service.receive(sent('x','/new'));await done;
  assert.deepEqual([kit.store.get(task.id).status,kit.store.taskForMessage('t','m2').status],['INTERRUPTED','CANCELLED']);assert.equal(kit.store.busy('t','u1'),false);
  // A Codex that cannot start: the first task fails before any turn, and what waited for it runs on its own instead.
  const broken=setup(async function*(){throw Error('spawn codex ENOENT');});
  const first=broken.store.create(input('m1','u1')).task;broken.store.transition(first.id,'STARTING',{started_at:Date.now()});
  await broken.service.receive(sent('m2','第二件'));await broken.service.start(broken.store.get(first.id));
  assert.equal(broken.store.get(first.id).status,'FAILED');await until(()=>broken.store.taskForMessage('t','m2').status==='QUEUED');
  kit.store.close();broken.store.close();
});
test('two messages sent one right after the other, before the first has started: the scheduler starts the first, and the second goes into its turn',async()=>{
  const handed=[];
  const kit=setup(async function*(job){yield started('th');yield turnStarted;job.steerable(async input=>{handed.push(input[0].text);});await until(()=>handed.length);yield say('a',`都做了：${handed[0]}`);yield completed;});
  await kit.service.receive(sent('m1','上传素材'));await kit.service.receive(sent('m2','压缩包在这'));
  assert.deepEqual(['m1','m2'].map(m=>kit.store.taskForMessage('t',m).status),['QUEUED','STEERING']);
  const scheduler=new Scheduler(kit.service);scheduler.tick();
  await until(()=>kit.store.taskForMessage('t','m2').status==='STEERED');await until(()=>kit.store.taskForMessage('t','m1').status==='COMPLETED');await scheduler.stop();
  assert.equal(kit.store.taskForMessage('t','m1').result,'都做了：压缩包在这');assert.equal(kit.codex.jobs.length,1);
  kit.store.close();
});
test('a message that needs a resource lock the running task does not hold is not handed to it: it runs on its own, with the lock',async()=>{
  const saved=config.resources;config.resources=[{key:'printer',regex:/打印/}];
  try{
    const kit=setup(async function*(job){yield started('th');yield turnStarted;job.steerable(async()=>{});await until(()=>kit.store.taskForMessage('t','m2'));yield say('a','好');yield completed;});
    const {done}=await running(kit);
    await kit.service.receive(sent('m2','顺便打印合同'));assert.equal(kit.store.taskForMessage('t','m2').status,'QUEUED');
    await done;kit.store.close();
  }finally{config.resources=saved;}
});
test('a message sent while /new stops the running task is for the new conversation: it does not go into the stopped task, nor start before the conversation is replaced',async()=>{
  const kit=setup(async function*(job,n){
    yield started(n===1?'th-old':'th-new');yield turnStarted;job.steerable(async()=>{});
    // The stopped process takes a moment to go, so that /new is still waiting when the next message comes.
    if(n===1){await new Promise(r=>job.signal.addEventListener('abort',()=>setTimeout(r,300),{once:true}));throw aborted();}
    yield say('a','新对话里做好了');yield completed;
  });
  const {task}=await running(kit),scheduler=new Scheduler(kit.service);scheduler.start();
  const resetting=kit.service.receive(sent('x','/new'));
  await kit.service.receive(sent('m2','新对话的第一件'));assert.equal(kit.store.taskForMessage('t','m2').status,'QUEUED','not handed to the task being stopped');
  await resetting;await until(()=>kit.store.taskForMessage('t','m2').status==='COMPLETED');await scheduler.stop();
  assert.equal(kit.store.get(task.id).status,'INTERRUPTED');assert.equal(kit.codex.jobs[1].threadId,null,'started in the new conversation');
  kit.store.close();
});
test('a message sent meanwhile whose attachment cannot be taken is refused at once, as a message of its own would be',async()=>{
  const downloader={downloadResource:async()=>{throw new InputRejected('附件超过 100 MB 接收上限，请拆分后分别发送');}};
  const kit=setup(async function*(job){yield started('th');yield turnStarted;job.steerable(async()=>{});await until(()=>kit.store.taskForMessage('t','m2')?.status==='FAILED');yield say('a','好');yield completed;},{downloader});
  const {done}=await running(kit);
  await kit.service.receive(sent('m2','',{attachments:[{kind:'file',key:'z',name:'大.zip'}]}));
  await until(()=>kit.store.taskForMessage('t','m2').status==='FAILED');assert.equal(kit.store.taskForMessage('t','m2').error,'附件超过 100 MB 接收上限，请拆分后分别发送');
  await done;kit.store.close();
});
test('a turn left because one of its events could not be recorded is still closed: Codex does not go on after the task has failed',async()=>{
  let closed=false;
  const kit=setup(async function*(){try{yield started('th');yield turnStarted;await new Promise(()=>{});}finally{closed=true;}});
  kit.service.onEvent=()=>{throw Error('database is locked');};
  const task=await kit.run();assert.equal(task.status,'FAILED');await until(()=>closed);
  kit.store.close();
});
test('a restart cancels a message that was being handed over, as it does one that was queued',()=>{
  const store=new Store(':memory:'),task=store.create(input('m1','u1')).task,waiting=store.create(input('m2','u1')).task;
  store.transition(task.id,'RUNNING');assert(store.claim(waiting.id,'QUEUED','STEERING'));assert(!store.claim(waiting.id,'QUEUED','STEERING'),'claimed once');
  assert.deepEqual(store.active().map(t=>t.id),[task.id],'a message being handed over has not started');assert(store.busy('t','u1'));
  store.discardLeftovers();
  assert.deepEqual([store.get(task.id).status,store.get(waiting.id).status],['FAILED','CANCELLED']);store.close();
});
