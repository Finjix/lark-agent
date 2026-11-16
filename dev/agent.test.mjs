import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import fsSync from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {ModelRuntime} from '@earendil-works/pi-coding-agent';
import {fauxProvider,fauxAssistantMessage,fauxToolCall,fauxText} from '@earendil-works/pi-ai';
import {config} from '../dist/config.js';
import {Store} from '../dist/storage/database.js';
import {TaskService} from '../dist/tasks/service.js';
import {Scheduler} from '../dist/tasks/scheduler.js';
import {Turn} from '../dist/agent/turn.js';
import {cardJson,cardElements,renderTask} from '../dist/feishu/cards.js';
import {agentInput,MAX_ATTACHED_IMAGE_BYTES} from '../dist/agent/input.js';
import {AgentRunner,SessionMissing,SteerRefused} from '../dist/agent/runner.js';
import {skillEnv,withPath,pythonFolders} from '../dist/agent/environment.js';
import {agentInstructions} from '../dist/agent/instructions.js';
import {BOT_DOWN,InputRejected} from '../dist/types.js';

// ---- what the runner reports of a turn (see agent/turn.ts)
const started=id=>({type:'session',id});
const say=(id,text,commentary=false)=>({type:'message',id,text,commentary});
const bash=(id,command,status,output,exit)=>({type:'tool',id,name:'bash',args:{command},status,...(output!==undefined?{output}:{}),...(exit!==undefined?{exit}:{})});
const completed={type:'completed'};
const failed=error=>({type:'failed',error});
const aborted=()=>Object.assign(Error('The operation was aborted'),{name:'AbortError'});
const untilAborted=signal=>new Promise((_,reject)=>signal.aborted?reject(aborted()):signal.addEventListener('abort',()=>reject(aborted()),{once:true}));
const until=async(condition)=>{for(let i=0;i<400;i++){if(condition())return;await new Promise(r=>setTimeout(r,5));}throw Error('condition never became true');};
/** A runner whose turns are whatever `script(job,n)` yields, recording every job it is given. */
function fakeAgent(script){
  const agent={ready:true,jobs:[]};
  agent.run=async function*(job){agent.jobs.push(job);yield* script(job,agent.jobs.length);};
  return agent;
}
const input=(message='m1',user='u1',instruction='帮我做点事')=>({tenant_key:'t',user_id:user,chat_id:'c',source_message_id:message,instruction});
function setup(script,{downloader}={}){
  const store=new Store(':memory:'),agent=fakeAgent(script),service=new TaskService(store,agent,downloader);service.authorized=()=>true;
  const run=async(message='m1',user='u1',instruction)=>{
    const task=store.create(input(message,user,instruction)).task;store.transition(task.id,'STARTING',{started_at:Date.now()});
    await service.start(store.get(task.id));return store.get(task.id);
  };
  return {store,agent,service,run};
}
const notices=store=>store.db.prepare("SELECT payload FROM delivery_outbox WHERE kind='NOTICE' ORDER BY rowid").all().map(r=>JSON.parse(r.payload).text);

// ---- what a turn looks like on a card
test('when the user added something after the agent\'s last work and it answered that too, the answer is every message since then',()=>{
  const turn=new Turn(),command=bash('c','ls','done','',0);
  turn.apply(say('m1','先看看',true));turn.apply(command);turn.steered('steer:m2','再加个封面',[]);turn.apply(say('m2','报告做好了：report.docx'));turn.apply(say('m3','封面也加好了。'));turn.apply(completed);
  assert.deepEqual([turn.answer,turn.progress],['报告做好了：report.docx\n\n封面也加好了。','先看看'],'the first request\'s answer is not pushed into the progress');
  const worked=new Turn();worked.steered('steer:m2','再加个封面',[]);worked.apply(command);worked.apply(say('m1','中间说明',true));worked.apply(bash('c2','ls','done','',0));worked.apply(say('m2','两件都做好了'));worked.apply(completed);
  assert.deepEqual([worked.answer,worked.progress],['两件都做好了','中间说明'],'taken in while the agent worked: the last message answers, as without it');
});
test('while a turn runs every message is progress; when it completes the last one is the answer',()=>{
  const turn=new Turn();
  assert.equal(turn.apply(started('s1')),false);assert.equal(turn.sessionId,'s1');
  assert.equal(turn.apply(say('a','先看看文件',true)),true);assert.equal(turn.apply(say('a','先看看文件',true)),false,'the same message again changes nothing');
  assert.equal(turn.apply(bash('c','ls','running')),true,'a command that starts is a step of its own');assert.equal(turn.apply(bash('c','ls','running')),false);
  turn.apply(say('b','文件在这里'));
  assert.deepEqual([turn.progress,turn.answer,turn.state],['先看看文件\n\n文件在这里','','running']);
  turn.apply(completed);
  assert.deepEqual([turn.progress,turn.answer,turn.state,turn.error],['先看看文件','文件在这里','completed','']);
  assert.deepEqual(turn.steps.map(x=>x.type),['message','command','message'],'in the order the agent took them');assert.equal(turn.progress.includes('ls'),false,'a command is not part of the text');
  const empty=new Turn();assert.equal(empty.apply(say('e','')),false,'a message with no text (only tool calls) is no step');
});
test('a failed turn keeps what was said and reports why',()=>{
  const turn=new Turn();turn.apply(say('a','做到一半',true));
  turn.apply(failed('You\'ve hit your usage limit'));
  assert.deepEqual([turn.state,turn.error,turn.progress,turn.answer],['failed','You\'ve hit your usage limit','做到一半','']);
});
test('bash, write and edit become commands and file changes, other tools tools; a step is updated in place and long output is cut from the front',()=>{
  const turn=new Turn();
  turn.apply(bash('c1','ls -la','running'));assert.deepEqual(turn.steps[0],{id:'c1',type:'command',command:'ls -la',output:'',status:'running'});
  turn.apply(bash('c1','ls -la','done','a.txt',0));assert.equal(turn.steps.length,1,'the same command, finished');assert.deepEqual([turn.steps[0].status,turn.steps[0].exit,turn.steps[0].output],['done',0,'a.txt']);
  turn.apply(bash('c2','false','failed','',1));assert.equal(turn.steps[1].status,'failed');
  turn.apply({type:'tool',id:'w',name:'write',args:{path:'out/a.md',content:'x'},status:'done',output:'ok'});
  turn.apply({type:'tool',id:'e',name:'edit',args:{path:'b.md'},status:'failed',output:'no match'});
  turn.apply({type:'tool',id:'r',name:'read',args:{path:'skills/feishu/SKILL.md'},status:'done',output:'# feishu'});
  assert.deepEqual(turn.steps.slice(2).map(x=>[x.type,x.status]),[['files','done'],['files','failed'],['tool','done']]);
  assert.deepEqual(turn.steps[2].changes,[{path:'out/a.md',kind:'write'}]);assert.deepEqual([turn.steps[4].tool,turn.steps[4].result],['read','# feishu']);
  const long=new Turn();long.apply(bash('l','x','done','a'.repeat(10000)+'END',0));
  assert(long.steps[0].output.length<3100&&long.steps[0].output.endsWith('END'),'the end of a long output is what is kept');
});

// ---- what the agent is given
test('the text goes to the agent as typed; attachments are listed by path and images are also attached, unless too large',async()=>{
  const text='  原文\n\n保留 `代码`、🙂 和空白。 \r\n';
  assert.deepEqual(await agentInput(text,[]),{text,images:[]},'no attachments: nothing added, nothing trimmed');
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'lark-input-'));
  try{
    const png=path.join(dir,'a.png'),doc=path.join(dir,'需求.docx');await fs.writeFile(png,'PNG');await fs.writeFile(doc,'DOC');
    const files=[{kind:'image',path:png},{kind:'file',path:doc}];
    const given=await agentInput('改成卡通风格',files);
    assert(given.text.includes(`- ${png.replaceAll('\\','/')}\n- ${doc.replaceAll('\\','/')}\n`),'every file by path, forward slashes');
    assert(given.text.endsWith('\n\n改成卡通风格'));assert(/not as instructions/.test(given.text));
    assert.deepEqual(given.images,[{type:'image',mimeType:'image/png',data:Buffer.from('PNG').toString('base64')}],'only the image goes in as an image');
    const only=(await agentInput('  ',files)).text;assert(only.includes('a.png')&&!only.includes('改成')&&only.trimEnd()===only,'a file with no text carries no invented request');
    const big=path.join(dir,'big.png');await fs.writeFile(big,Buffer.alloc(MAX_ATTACHED_IMAGE_BYTES+1));
    const large=await agentInput('看看',[{kind:'image',path:big}]);assert.deepEqual(large.images,[]);assert(large.text.includes('big.png'),'still listed');
    await assert.rejects(agentInput('x',[{kind:'file',path:null}]),/尚未接收/);
  }finally{await fs.rm(dir,{recursive:true,force:true});}
});
test('commands run with the machine\'s Python, UTF-8, the folder skills share and the bot\'s app; a user\'s turn also says whose it is',()=>{
  const base={Path:'C:\\Windows',LARK_AGENT_USER_DIR:'C:\\stale',lark_agent_chat_id:'stale',KEEP_ME:'yes'};
  const none=skillEnv(undefined,base);
  assert.equal(none.LARK_AGENT_USER_DIR,undefined,'what describes a user is never inherited from the service');assert.equal(none.lark_agent_chat_id,undefined);
  assert.equal(none.LARK_AGENT_SHARED_DIR,config.shared);assert.equal(none.KEEP_ME,'yes');assert.equal(none.PYTHONUTF8,'1');
  assert.equal(none.Path.split(path.delimiter).at(-1),'C:\\Windows','the service\'s PATH is kept');
  assert.deepEqual([pythonFolders('python'),pythonFolders('C:\\Python313\\python.exe')],[[],['C:\\Python313']],'a Python given by full path goes in front of PATH');
  const user=skillEnv({dir:'D:/p/user/张三',openId:'ou_1',chat:'oc_1'},base);
  assert.deepEqual([user.LARK_AGENT_USER_DIR,user.LARK_AGENT_USER_OPEN_ID,user.LARK_AGENT_CHAT_ID,user.LARK_AGENT_CHAT_TYPE],['D:/p/user/张三','ou_1','oc_1',undefined],'a value not known is left out');
  assert.deepEqual(withPath({Path:'C:\\x',PATH:'C:\\y',other:'1'},['C:\\py'],'win32'),{Path:'C:\\py;C:\\x',other:'1'},'one spelling of PATH on Windows, the tools in front');
});
test('the agent is told to work only through the skills and to do no more on the machine than using them takes',()=>{
  const none=agentInstructions([]),some=agentInstructions([{name:'feishu'},{name:'audit-auto'}]);
  assert.match(none,/No skills are installed/);assert.doesNotMatch(none,/Installed skills/);
  assert.match(some,/Installed skills: feishu, audit-auto\./);assert.doesNotMatch(some,/No skills are installed/);
  for(const text of [none,some]){assert.match(text,/only with the skills/);assert.match(text,/read that skill's SKILL\.md/);assert.match(text,/cannot be done without it/);
    assert.match(text,/environment variables, keys, credentials/);assert.match(text,/which skill you are using/);assert.match(text,/Git Bash/);
    assert.match(text,/not a request to run a skill on them[^]*each thing you could do with them[^]*only once the user has chosen it/);}
});

// ---- the runner, against pi's fake provider
async function fakeRunner(){
  const home=await fs.mkdtemp(path.join(os.tmpdir(),'lark-pi-')),skills=path.join(home,'skills'),cwd=path.join(home,'ws');
  await fs.mkdir(path.join(skills,'demo'),{recursive:true});await fs.mkdir(cwd);
  await fs.writeFile(path.join(skills,'demo','SKILL.md'),'---\nname: demo\ndescription: A demo skill. Use it when the user asks for a demo.\n---\n\nRun scripts/demo.py.\n');
  const runtime=await ModelRuntime.create({authPath:path.join(home,'auth.json'),modelsPath:null,refreshOnCreate:false});
  const faux=fauxProvider({provider:'faux',models:[{id:'m1',input:['text','image']}]});runtime.registerNativeProvider(faux.provider);
  await runtime.setRuntimeApiKey('faux','test-key');
  const runner=new AgentRunner({runtime:async()=>runtime,provider:'faux',model:'m1',thinking:'off',skillsDir:skills,sessionsDir:path.join(home,'sessions')});
  return {runner,faux,home,cwd,done:()=>fs.rm(home,{recursive:true,force:true}).catch(()=>{})};
}
const collect=async(runner,job)=>{const events=[];for await(const event of runner.run({sessionId:null,input:{text:'hi',images:[]},signal:new AbortController().signal,...job}))events.push(event);return events;};
const lastUser=context=>context.messages.filter(m=>m.role==='user').at(-1);
const textIn=message=>typeof message.content==='string'?message.content:message.content.filter(c=>c.type==='text').map(c=>c.text).join('');

test('the agent is ready once its model is known and has a login',async()=>{
  const {runner,home,done}=await fakeRunner();
  try{
    const codex=new AgentRunner({runtime:()=>runner.models(),provider:'openai-codex',model:'gpt-6.1-sol'});
    assert.equal(await codex.probe(),false,'no ChatGPT login yet');assert.match(codex.error,/start\.cmd login/);
    // What `start.cmd login` leaves in auth.json (pi notices the file has changed).
    await fs.writeFile(path.join(home,'auth.json'),JSON.stringify({'openai-codex':{type:'oauth',access:'a',refresh:'r',expires:Date.now()+3600000}}));
    assert.equal(await codex.probe(),true);assert.equal(codex.error,'');
    const unknown=new AgentRunner({runtime:()=>runner.models(),provider:'faux',model:'nope'});assert.equal(await unknown.probe(),false);assert.match(unknown.error,/AGENT_MODEL/);
  }finally{await done();}
});
test('a turn runs with the skills and the instructions, its commands have the user\'s environment, and the next turn continues the chat',async()=>{
  const {runner,faux,cwd,done}=await fakeRunner();
  try{
    let seen;
    faux.setResponses([
      context=>{seen=context;return fauxAssistantMessage([fauxText('先查一下'),fauxToolCall('bash',{command:'echo "$LARK_AGENT_USER_OPEN_ID|$PYTHONUTF8"'})],{stopReason:'toolUse'});},
      fauxAssistantMessage('查好了'),
    ]);
    const events=await collect(runner,{cwd,user:{dir:'D:/u/张三',openId:'ou_zs',chat:'oc_1'}});
    assert.deepEqual(events.map(e=>e.type),['session','message','tool','tool','message','completed']);
    const [session,first,start,end,answer]=events;
    assert.deepEqual([first.text,first.commentary,answer.text,answer.commentary],['先查一下',true,'查好了',false],'a message with tool calls is on the way to the answer');
    assert.deepEqual([start.name,start.status,start.args.command],['bash','running','echo "$LARK_AGENT_USER_OPEN_ID|$PYTHONUTF8"']);
    assert.deepEqual([end.status,end.output.trim(),end.exit,end.args.command],['done','ou_zs|1',0,start.args.command],'the command ran with the user\'s environment, and its end says what it was');
    const prompt=JSON.stringify(seen);
    assert.match(prompt,/Installed skills: demo/);assert.match(prompt,/A demo skill/);assert.match(prompt,/demo[\\/]+SKILL\.md/,'pi says where the skill is');
    assert.doesNotMatch(prompt,/coding assistant running inside pi/,'pi\'s own preamble is replaced');
    assert.deepEqual(runner.skills.map(s=>s.name),['demo']);
    faux.setResponses([context=>fauxAssistantMessage(`第 ${context.messages.filter(m=>m.role==='user').length} 条`)]);
    const again=await collect(runner,{cwd,sessionId:session.id,input:{text:'再来',images:[]}});
    assert.deepEqual([again[0].id,again.at(-2).text],[session.id,'第 2 条'],'the same chat, with what was said before');
    await assert.rejects(collect(runner,{cwd,sessionId:'no-such-session'}),SessionMissing);
  }finally{await done();}
});
test('images go to the model with the message; a failed request is a failed turn',async()=>{
  const {runner,faux,cwd,done}=await fakeRunner();
  try{
    let images;
    faux.setResponses([context=>{images=lastUser(context).content.filter(c=>c.type==='image');return fauxAssistantMessage('看到了');},
      fauxAssistantMessage('',{stopReason:'error',errorMessage:'You\'ve hit your usage limit'})]);
    // A real image: pi resizes what it sends, and drops what it cannot read.
    const data='iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEElEQVR4nGP4z8AARAwQCgAf7gP9i18U1AAAAABJRU5ErkJggg==';
    await collect(runner,{cwd,input:{text:'看图',images:[{type:'image',mimeType:'image/png',data}]}});
    assert.equal(images.length,1);
    const events=await collect(runner,{cwd});assert.deepEqual(events.at(-1),{type:'failed',error:'You\'ve hit your usage limit'});
  }finally{await done();}
});
test('a message handed to a running turn is taken after the current step and answered in it; once the turn is over a hand-over is refused',async()=>{
  const {runner,faux,cwd,done}=await fakeRunner();
  try{
    let steer,offers=[],answered;
    faux.setResponses([fauxAssistantMessage([fauxToolCall('bash',{command:'sleep 1'})],{stopReason:'toolUse'}),
      context=>{answered=textIn(lastUser(context));return fauxAssistantMessage(`也做了：${answered}`);}]);
    const running=collect(runner,{cwd,steerable:fn=>{offers.push(!!fn);if(fn)steer=fn;}});
    await until(()=>steer);
    await steer({text:'再加一件',images:[]});
    const events=await running;
    assert.equal(answered,'再加一件');assert.equal(events.at(-2).text,'也做了：再加一件');assert.deepEqual(offers,[true,false],'withdrawn once the turn is over');
    await assert.rejects(steer({text:'太晚了',images:[]}),SteerRefused);
  }finally{await done();}
});
test('a hand-over pi rewrites as it takes it (a /skill: line, an image it resized) still counts as taken, and several are taken in order',async()=>{
  const {runner,faux,cwd,done}=await fakeRunner();
  try{
    let steer,users;
    faux.setResponses([fauxAssistantMessage([fauxToolCall('bash',{command:'sleep 1'})],{stopReason:'toolUse'}),
      context=>{users=context.messages.filter(m=>m.role==='user').map(textIn);return fauxAssistantMessage('好');}]);
    const running=collect(runner,{cwd,steerable:fn=>{if(fn)steer=fn;}});
    await until(()=>steer);
    await Promise.all([steer({text:'/skill:demo 再做一个',images:[]}),steer({text:'第三件',images:[]})]);
    await running;
    assert.equal(users.length,3,'the prompt and both hand-overs reached the model');assert.notEqual(users[1],'/skill:demo 再做一个','pi expanded it');assert.equal(users[2],'第三件');
  }finally{await done();}
});
test('a stop that comes while the turn is getting ready is not lost: nothing runs',async()=>{
  const {runner,faux,cwd,done}=await fakeRunner();
  try{
    const marker=path.join(cwd,'ran.txt');
    faux.setResponses([fauxAssistantMessage([fauxToolCall('bash',{command:'echo x > ran.txt'})],{stopReason:'toolUse'}),fauxAssistantMessage('不会到这里')]);
    const controller=new AbortController(),seen=[];
    const running=(async()=>{for await(const event of runner.run({sessionId:null,cwd,input:{text:'做',images:[]},signal:controller.signal})){seen.push(event.type);if(event.type==='session')controller.abort();}})();
    await assert.rejects(running,{name:'AbortError'});
    assert(!seen.includes('tool')&&!seen.includes('completed'));assert(!fsSync.existsSync(marker),'the command never ran');
    const early=new AbortController();early.abort();
    await assert.rejects(collect(runner,{cwd,signal:early.signal}),{name:'AbortError'});
  }finally{await done();}
});
test('a chat is found by its id even after the project moved',async()=>{
  const {runner,faux,home,cwd,done}=await fakeRunner();
  try{
    faux.setResponses([fauxAssistantMessage('一'),context=>fauxAssistantMessage(`第 ${context.messages.filter(m=>m.role==='user').length} 条`)]);
    const [session]=await collect(runner,{cwd});
    const moved=path.join(home,'moved-ws');await fs.mkdir(moved);
    const again=await collect(runner,{cwd:moved,sessionId:session.id});
    assert.equal(again.at(-2).text,'第 2 条');
  }finally{await done();}
});
test('a turn stopped mid-command ends at once, the command with it',async()=>{
  const {runner,faux,cwd,done}=await fakeRunner();
  try{
    faux.setResponses([fauxAssistantMessage([fauxToolCall('bash',{command:'sleep 30'})],{stopReason:'toolUse'}),fauxAssistantMessage('不会到这里')]);
    const controller=new AbortController(),since=Date.now(),seen=[];
    const running=(async()=>{for await(const event of runner.run({sessionId:null,cwd,input:{text:'等等',images:[]},signal:controller.signal})){seen.push(event);if(event.type==='tool')controller.abort();}})();
    await assert.rejects(running,{name:'AbortError'});
    assert(Date.now()-since<10000,'not held until the command would have ended');assert(!seen.some(e=>e.type==='completed'));
  }finally{await done();}
});

// ---- a task from message to result
test('a task runs in the user\'s workspace as a new chat, is bound to it, and ends with the answer; the next task resumes that chat',async()=>{
  const {store,agent,run}=setup(async function*(job,n){
    yield started(job.sessionId??`thread-${n}`);yield say('a','我先看一下');yield say('b',`第${n}次的答案`);yield completed;
  });
  const first=await run('m1');
  assert.equal(first.status,'COMPLETED');assert.equal(first.result,'第1次的答案');assert.equal(first.progress,'我先看一下');assert.equal(first.error,'');
  const job=agent.jobs[0],session=store.session(first.session_id);
  assert.equal(job.sessionId,null,'a first message starts a chat');assert.deepEqual(job.input,{text:'帮我做点事',images:[]});
  assert.equal(session.thread_id,'thread-1');assert.equal(first.thread_id,'thread-1');
  assert.equal(job.cwd,session.workspace_path);assert.equal(path.basename(job.cwd),'workspace');assert(job.cwd.startsWith(config.user),'inside user/');
  assert.equal(first.workspace_path,job.cwd,'what the agent makes in its working folder is what is delivered');assert((await fs.stat(first.workspace_path)).isDirectory(),'the folder exists before the agent starts');
  assert(store.submitted(first.id));
  const second=await run('m2');
  assert.equal(agent.jobs[1].sessionId,'thread-1','the same chat continues');assert.equal(agent.jobs[1].cwd,job.cwd);
  assert.equal(second.result,'第2次的答案');assert.equal(store.session(second.session_id).thread_id,'thread-1','a resumed chat keeps its binding');
  assert.equal(store.sessions().length,1);store.close();
});

test('what the agent says shows up while the task runs, and the user\'s queue is held until it ends',async()=>{
  let release;const gate=new Promise(r=>release=r);
  const {store,service}=setup(async function*(){yield started('th');yield say('a','第一步完成');await gate;yield say('b','最后的答案');yield completed;});
  const task=store.create(input()).task;store.transition(task.id,'STARTING',{started_at:Date.now()});
  const running=service.start(store.get(task.id));
  await until(()=>store.get(task.id).progress==='第一步完成');
  assert.equal(store.get(task.id).status,'RUNNING');assert.equal(store.get(task.id).result,'');assert(store.busy('t','u1'));
  const row=store.db.prepare('SELECT status FROM delivery_outbox WHERE id=?').get(`view:${task.id}`);assert.equal(row.status,'PENDING','the card is refreshed');
  release();await running;assert.equal(store.get(task.id).status,'COMPLETED');assert.equal(store.busy('t','u1'),false);store.close();
});

test('a task ends when the agent reports the result, even if something the turn started keeps it winding down; it is then stopped',async()=>{
  let stopped=false;
  const {store,service,run}=setup(async function*(job){
    yield started('th');yield say('a','请扫码');yield completed;
    // A command that left a browser waiting for a QR scan: the result is out, but the turn does not wind down.
    job.signal.addEventListener('abort',()=>{stopped=true;},{once:true});await new Promise(()=>{});
  });
  service.exitWaitMs=20;
  const task=await run();
  assert.equal(task.status,'COMPLETED');assert.equal(task.result,'请扫码');assert(stopped,'the turn is stopped');
  store.close();
});

test('a failed turn shows the model\'s reason even if winding down then fails; any other way a run goes wrong is the one bot-down sentence',async()=>{
  let mode;
  const {store,run}=setup(async function*(job,n){
    yield started(`th-${n}`);
    if(mode==='failed'){yield say('a','做了一半');yield failed('You\'ve hit your usage limit');throw Error('winding down failed');}
    if(mode==='auth'){yield failed('401 Unauthorized: token expired, please log in again');return;}
    if(mode==='crash')throw Error('boom');
    if(mode==='silent')return;
    yield completed;
  });
  mode='failed';let task=await run('m1','u1');assert.equal(task.status,'FAILED');assert.equal(task.error,'You\'ve hit your usage limit');assert.equal(task.progress,'做了一半');
  mode='auth';task=await run('m4','u4');assert.equal(task.status,'FAILED');assert.equal(task.error,BOT_DOWN,'a login the provider no longer accepts is the bot being down, not a message for the user');
  mode='crash';task=await run('m2','u2');assert.equal(task.status,'FAILED');assert.equal(task.error,BOT_DOWN);
  mode='silent';task=await run('m3','u3');assert.equal(task.status,'FAILED');assert.equal(task.error,BOT_DOWN,'a run that ends without a result is not a success');
  store.close();
});

test('a chat the agent no longer has is replaced before anything is sent, once; any other failure is not mistaken for it',async()=>{
  const missing=new SessionMissing('th-old');
  let behaviour;
  const {store,agent,run}=setup(async function*(job,n){
    if(job.sessionId?.startsWith('th-old')&&behaviour==='missing')throw missing;
    if(job.sessionId?.startsWith('th-old')&&behaviour==='late'){yield started(job.sessionId);throw missing;}
    yield started(`th-${n}`);yield say('a','好的');yield completed;
  });
  behaviour='ok';const first=await run('m1');const old=store.session(first.session_id);
  store.db.prepare('UPDATE user_sessions SET thread_id=? WHERE id=?').run('th-old',old.id);
  behaviour='missing';const second=await run('m2');
  assert.equal(second.status,'COMPLETED');assert.deepEqual(agent.jobs.slice(1).map(j=>j.sessionId),['th-old',null],'resumed, then started anew');
  const now=store.session(second.session_id);assert.notEqual(now.id,old.id);assert(store.session(old.id).retired_at);assert.equal(now.workspace_path,old.workspace_path,'same workspace');
  assert.equal(second.thread_id,now.thread_id);assert(second.thread_id);
  assert.equal(store.db.prepare("SELECT count(*) c FROM task_events WHERE task_id=? AND kind='SESSION_REPLACED'").get(second.id).c,1);
  // Once the agent has said anything the same error is a real failure, and the chat is not swapped under the user.
  store.db.prepare('UPDATE user_sessions SET thread_id=? WHERE id=?').run('th-old-2',now.id);
  behaviour='late';const third=await run('m3');assert.equal(third.status,'FAILED');assert.equal(store.session(now.id).retired_at,null);
  store.close();
});

test('/new while a task runs stops its turn, keeps what it said, and starts a new chat; a turn that will not stop does not hold the user',async()=>{
  const {store,agent,service}=setup(async function*(job,n){yield started(`th-${n}`);yield say('a','做了一点');await untilAborted(job.signal);});
  const task=store.create(input()).task;store.transition(task.id,'STARTING',{started_at:Date.now()});
  const running=service.start(store.get(task.id));await until(()=>store.get(task.id).progress==='做了一点');
  const message=(text,id,user='u1')=>({tenant:'t',user,chat:'c',message:id,text});
  const old=store.session(store.get(task.id).session_id);
  await service.receive(message('/new','c1'));await running;
  const after=store.get(task.id);assert.equal(after.status,'INTERRUPTED');assert.equal(after.progress,'做了一点');assert.equal(after.cancel_requested,1);
  assert.match(notices(store).at(-1),/新的对话/);assert.equal(store.busy('t','u1'),false,'the next input is accepted');assert(store.session(old.id).retired_at,'and it starts a new chat');
  // A turn that ignores the abort: /new ends the task anyway, once it has waited a moment.
  const stuck=setup(async function*(){yield started('th');await new Promise(()=>{});});stuck.service.stopWaitMs=30;stuck.service.exitWaitMs=60;
  const t2=stuck.store.create(input()).task;stuck.store.transition(t2.id,'STARTING',{started_at:Date.now()});const hanging=stuck.service.start(stuck.store.get(t2.id));await until(()=>stuck.store.get(t2.id).status==='RUNNING');
  await stuck.service.receive(message('/new','c1'));assert.equal(stuck.store.get(t2.id).status,'INTERRUPTED');assert.equal(stuck.store.busy('t','u1'),false);
  await hanging;assert.equal(stuck.service.interrupt(stuck.store.get(t2.id)),false,'and its run is over too, not left waiting for events that never come');
  store.close();stuck.store.close();
});

test('a task stopped mid-turn ends even when its events never do (a browser left waiting for a QR scan): its time limit cannot leave the user blocked',async()=>{
  let killed=false;
  const {store,service}=setup(async function*(job){
    yield started('th');yield say('a','请扫码登录');
    // Stopping the turn does not end its events while something it left running holds it.
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
test('a task cancelled before the agent was started is cancelled, not interrupted; /new ends a queued one and a leftover nobody runs',async()=>{
  const {store,agent,service}=setup(async function*(){yield started('th');yield completed;});
  const task=store.create(input()).task;store.transition(task.id,'STARTING',{started_at:Date.now()});store.update(task.id,{cancel_requested:1});
  await service.start(store.get(task.id));assert.equal(store.get(task.id).status,'CANCELLED');assert.equal(agent.jobs.length,0);
  const queued=store.create(input('m2','u2')).task,leftover=store.create(input('m3','u3')).task;store.transition(leftover.id,'RUNNING');
  const message=(text,id,user)=>({tenant:'t',user,chat:'c',message:id,text});
  await service.receive(message('/new','x1','u2'));assert.equal(store.get(queued.id).status,'CANCELLED');
  await service.receive(message('/new','x2','u3'));assert.equal(store.get(leftover.id).status,'INTERRUPTED','nothing runs it, so nothing can answer');
  assert(!store.busy('t','u2')&&!store.busy('t','u3'));store.close();
});
test('the bot answers /new and nothing else: other slashes are the user\'s words for the agent',async()=>{
  const {store,agent,service}=setup(async function*(){yield started('th');yield completed;});
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
    service.agent.ready=true;const task=store.create(input()).task;const scheduler=new Scheduler(service);scheduler.tick();
    await until(()=>store.get(task.id).status==='RUNNING');await new Promise(r=>setTimeout(r,80));scheduler.tick();
    await until(()=>store.get(task.id).status==='INTERRUPTED');assert.equal(store.get(task.id).cancel_requested,1);await scheduler.stop();
  }finally{config.TASK_TIMEOUT_MS=saved;store.close();}
});

test('stopping the service ends every turn and finishes its task as failed, so no card keeps saying "running"',async()=>{
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
  const {store,agent,service,run}=setup(async function*(job,n){yield started(`th-${n}`);yield say('a','ok');yield completed;});
  const message=(text,id)=>({tenant:'t',user:'u1',chat:'c',message:id,text});
  await service.receive(message('/new','n0'));assert.match(notices(store).at(-1),/还没有对话/);
  const first=await run('m1');const old=store.session(first.session_id);
  await service.receive(message('/new','n2'));assert.match(notices(store).at(-1),/新的对话/);assert(store.session(old.id).retired_at);
  const next=await run('m2');assert.equal(agent.jobs.at(-1).sessionId,null);assert.notEqual(next.session_id,old.id);assert.equal(store.session(next.session_id).workspace_path,old.workspace_path);
  assert.equal(store.session(old.id).thread_id,'th-1','the old chat is only set aside');store.close();
});

// ---- attachments, images
test('attachments are saved in the workspace before the agent starts; images go in as images, every file by path',async()=>{
  const downloader={downloadResource:async(_message,attachment,file)=>{await fs.writeFile(file,'data');return {size:4,contentType:attachment.kind==='image'?'image/png':'application/pdf'};}};
  const {store,agent,service}=setup(async function*(){yield started('th');yield say('a','看到了');yield completed;},{downloader});
  const task=store.create(input('m1','u1','总结一下'),[],[{kind:'image',key:'i',name:'图.png'},{kind:'file',key:'f',name:'报告.pdf'}]).task;
  store.transition(task.id,'STARTING',{started_at:Date.now()});await service.start(store.get(task.id));
  const done=store.get(task.id);assert.equal(done.status,'COMPLETED');assert.equal(done.progress,'','the "receiving attachments" label does not linger');
  const job=agent.jobs[0],{text,images}=job.input,saved=store.inputs(task.id).map(i=>i.path),realWorkspace=await fs.realpath(job.cwd);
  assert(saved.every(file=>path.dirname(file)===path.join(realWorkspace,'input')),"in the workspace's input/ folder");
  assert(saved.every(file=>text.includes(file.replaceAll('\\','/'))));assert(text.endsWith('\n\n总结一下'));
  assert.deepEqual(images,[{type:'image',mimeType:'image/png',data:Buffer.from('data').toString('base64')}]);assert.equal(await fs.readFile(saved[1],'utf8'),'data');
  // A download that fails ends the task before the agent hears of it.
  const bad=setup(async function*(){assert.fail('the agent must not start');},{downloader:{downloadResource:async()=>{throw Error('缺少 im:resource 权限');}}});
  const t2=bad.store.create(input('m1','u1'),[],[{kind:'file',key:'f',name:'a.pdf'}]).task;bad.store.transition(t2.id,'STARTING');await bad.service.start(bad.store.get(t2.id));
  assert.equal(bad.store.get(t2.id).status,'FAILED');assert.equal(bad.store.get(t2.id).error,BOT_DOWN);assert.equal(bad.agent.jobs.length,0);assert(!bad.store.submitted(t2.id));
  store.close();bad.store.close();
});


// ---- the steps of a turn on a card
test('the card lists the steps as the agent took them: commands, file changes and tool calls are not shown, long notes fold, the answer stays open, and the whole card fits',()=>{
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
  const {store,service}=setup(async function*(){yield started('th');yield bash('c','dir','running');await gate;
    yield bash('c','dir','done','a.txt',0);yield say('m','好了');yield completed;});
  const task=store.create(input()).task;store.transition(task.id,'STARTING',{started_at:Date.now()});const running=service.start(store.get(task.id));
  await until(()=>JSON.parse(store.get(task.id).steps).length===1);
  assert.equal(JSON.parse(store.get(task.id).steps)[0].status,'running');assert.equal(store.get(task.id).progress,'','no text yet, but the card shows the command');
  assert.equal(store.db.prepare('SELECT status FROM delivery_outbox WHERE id=?').get('view:'+task.id).status,'PENDING');
  release();await running;const done=store.get(task.id);assert.deepEqual(JSON.parse(done.steps).map(x=>[x.type,x.status??'']),[['command','done'],['message','']]);assert.equal(done.result,'好了');store.close();
});

test('every turn is handed the user it is for: their own folder (not the workspace in it), their Open ID and the chat',async()=>{
  const {agent,store,run}=setup(async function*(){yield started('th');yield say('a','好');yield completed;});
  const task=await run('m1','ou_alice'),job=agent.jobs[0],session=store.session(task.session_id);
  assert.equal(job.user.openId,'ou_alice');assert.equal(job.user.chat,task.chat_id);
  assert.equal(job.user.dir,path.dirname(session.workspace_path),'user/<name>, the parent of workspace/');
  assert(path.isAbsolute(job.user.dir)&&job.user.dir.startsWith(config.user));
  await run('m2','ou_bob');assert.notEqual(agent.jobs[1].user.dir,job.user.dir,'another user, another folder');assert.equal(agent.jobs[1].user.openId,'ou_bob');
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
    yield started('th');job.steerable(async input=>{handed.push(input);});
    yield {type:'message',id:'a',text:'先做第一件',commentary:true};await until(()=>handed.length);
    yield say('b',`都做好了，也${handed[0].text}`);yield completed;
  });
  const {task,done}=await running(kit);
  await kit.service.receive(sent('m2','加一张封面'));await done;
  const joined=kit.store.taskForMessage('t','m2'),after=kit.store.get(task.id),steps=JSON.parse(after.steps);
  assert.deepEqual([joined.status,after.status,after.result],['STEERED','COMPLETED','都做好了，也加一张封面']);
  assert.equal(kit.agent.jobs.length,1,'no turn of its own');assert.deepEqual(handed,[{text:'加一张封面',images:[]}],'given to the turn as typed');
  assert.deepEqual(steps.map(s=>s.type),['message','user','message'],'shown where it came in');
  assert.deepEqual(steps[1],{id:`steer:${joined.id}`,type:'user',text:'加一张封面',files:[]});
  assert(cardElements(after,renderTask(after)).some(e=>e.content==='➕ 补充：加一张封面'));
  assert.equal(renderTask(joined),'','nothing of its own to show');
  assert.equal(kit.store.db.prepare("SELECT count(*) c FROM task_events WHERE task_id=? AND kind='STEERED'").get(task.id).c,1);
  kit.store.close();
});
test('a message the turn no longer takes (it has just ended) goes back in the queue and runs on its own, in the same chat',async()=>{
  const kit=setup(async function*(job,n){
    yield started('th');job.steerable(async()=>{throw new SteerRefused();});
    if(n===1){yield say('a','第一件');await until(()=>kit.store.taskForMessage('t','m2')?.status==='QUEUED');}
    yield say('b',`答：${job.input.text}`);yield completed;
  });
  const {done}=await running(kit);
  await kit.service.receive(sent('m2','第二件'));await done;
  assert.equal(kit.store.taskForMessage('t','m2').status,'QUEUED');
  const scheduler=new Scheduler(kit.service);scheduler.tick();await until(()=>kit.store.taskForMessage('t','m2').status==='COMPLETED');await scheduler.stop();
  assert.equal(kit.store.taskForMessage('t','m2').result,'答：第二件');assert.equal(kit.agent.jobs.length,2);assert.equal(kit.agent.jobs[1].sessionId,'th');
  kit.store.close();
});
test('a message still being handed over when the turn ends runs before what the user sent after it',async()=>{
  let finish,release;const finished=new Promise(r=>{finish=r;}),arrived=new Promise(r=>{release=r;});
  const downloader={downloadResource:async(_message,_attachment,file)=>{await arrived;await fs.writeFile(file,'data');return {size:4,contentType:'application/zip'};}};
  const kit=setup(async function*(job,n){
    yield started('th');
    if(n===1){job.steerable(async()=>{throw new SteerRefused();});await finished;}
    yield say('a',`答：${job.input.text}`);yield completed;
  },{downloader});
  const {done}=await running(kit);
  // Its attachment is still arriving when the turn ends, and the user sends another message meanwhile.
  await kit.service.receive(sent('m2','第二件',{attachments:[{kind:'file',key:'z',name:'素材.zip'}]}));
  finish();await done;await kit.service.receive(sent('m3','第三件'));
  const scheduler=new Scheduler(kit.service);scheduler.tick();
  assert.deepEqual(['m2','m3'].map(m=>kit.store.taskForMessage('t',m).status),['STEERING','QUEUED'],'the later one waits behind it');
  release();await until(()=>{scheduler.tick();return kit.store.taskForMessage('t','m3').status==='COMPLETED';});await scheduler.stop();
  assert.equal(kit.agent.jobs.length,3);assert.match(kit.agent.jobs[1].input.text,/第二件$/);assert.equal(kit.agent.jobs[2].input.text,'第三件');
  kit.store.close();
});
test('a message sent before the previous one has started waits for its turn and goes into it; messages go in the order they came, each once',async()=>{
  const handed=[];
  const kit=setup(async function*(job){
    yield started('th');job.steerable(async input=>{handed.push(input.text);});
    await until(()=>handed.length===2);yield say('a',handed.join('+'));yield completed;
  });
  const first=kit.store.create(input('m1','u1','一')).task;
  await kit.service.receive(sent('m2','二'));await kit.service.receive(sent('m3','三'));await kit.service.receive(sent('m2','二'));
  assert.deepEqual(['m2','m3'].map(m=>kit.store.taskForMessage('t',m).status),['STEERING','STEERING'],'waiting, not queued: the scheduler leaves them alone');
  assert.equal(kit.store.list().length,3,'a redelivered event is no new message');
  kit.store.transition(first.id,'STARTING',{started_at:Date.now()});await kit.service.start(kit.store.get(first.id));
  await until(()=>kit.store.taskForMessage('t','m3').status==='STEERED');
  assert.deepEqual(handed,['二','三']);assert.equal(kit.store.get(first.id).result,'二+三');assert.equal(kit.agent.jobs.length,1);
  kit.store.close();
});
test('the files of a message sent meanwhile arrive in the workspace the turn works in, and go to the turn with it',async()=>{
  const downloader={downloadResource:async(_message,attachment,file)=>{await fs.writeFile(file,'data');return {size:4,contentType:attachment.kind==='image'?'image/png':'application/zip'};}};
  const handed=[];
  const kit=setup(async function*(job){yield started('th');job.steerable(async input=>{handed.push(input);});await until(()=>handed.length);yield say('a','收到');yield completed;},{downloader});
  const {task,done}=await running(kit);
  await kit.service.receive(sent('m2','',{attachments:[{kind:'file',key:'z',name:'玩法AI迭代素材.zip'},{kind:'image',key:'i',name:'封面.png'}]}));await done;
  const joined=kit.store.taskForMessage('t','m2'),files=kit.store.inputs(joined.id).map(i=>i.path),{text,images}=handed[0];
  assert.equal(joined.status,'STEERED');assert(files.every(f=>path.dirname(f)===path.join(fsSync.realpathSync(kit.store.get(task.id).workspace_path),'input')),"in the workspace's input/");
  assert(files.every(f=>text.includes(f.replaceAll('\\','/'))),'listed by path, as for a message of its own');assert.deepEqual(images.map(i=>i.mimeType),['image/png']);
  assert.deepEqual(JSON.parse(kit.store.get(task.id).steps).find(s=>s.type==='user'),{id:`steer:${joined.id}`,type:'user',text:'',files:['玩法AI迭代素材.zip','封面.png']});
  assert(cardElements(kit.store.get(task.id),' ').some(e=>e.content==='➕ 补充：（附件）\n📎 玩法AI迭代素材.zip、封面.png'));
  kit.store.close();
});
test('/new stops the running task and cancels what was waiting to go into it; a task that ends before its turn starts sends its waiting messages back to the queue',async()=>{
  const kit=setup(async function*(job){yield started('th');await untilAborted(job.signal);});
  const {task,done}=await running(kit);
  await kit.service.receive(sent('m2','第二件'));assert.equal(kit.store.taskForMessage('t','m2').status,'STEERING','the turn offers no way in yet');
  await kit.service.receive(sent('x','/new'));await done;
  assert.deepEqual([kit.store.get(task.id).status,kit.store.taskForMessage('t','m2').status],['INTERRUPTED','CANCELLED']);assert.equal(kit.store.busy('t','u1'),false);
  // An agent that cannot start: the first task fails before any turn, and what waited for it runs on its own instead.
  const broken=setup(async function*(){throw Error('No API key found for openai-codex');});
  const first=broken.store.create(input('m1','u1')).task;broken.store.transition(first.id,'STARTING',{started_at:Date.now()});
  await broken.service.receive(sent('m2','第二件'));await broken.service.start(broken.store.get(first.id));
  assert.equal(broken.store.get(first.id).status,'FAILED');await until(()=>broken.store.taskForMessage('t','m2').status==='QUEUED');
  kit.store.close();broken.store.close();
});
test('two messages sent one right after the other, before the first has started: the scheduler starts the first, and the second goes into its turn',async()=>{
  const handed=[];
  const kit=setup(async function*(job){yield started('th');job.steerable(async input=>{handed.push(input.text);});await until(()=>handed.length);yield say('a',`都做了：${handed[0]}`);yield completed;});
  await kit.service.receive(sent('m1','上传素材'));await kit.service.receive(sent('m2','压缩包在这'));
  assert.deepEqual(['m1','m2'].map(m=>kit.store.taskForMessage('t',m).status),['QUEUED','STEERING']);
  const scheduler=new Scheduler(kit.service);scheduler.tick();
  await until(()=>kit.store.taskForMessage('t','m2').status==='STEERED');await until(()=>kit.store.taskForMessage('t','m1').status==='COMPLETED');await scheduler.stop();
  assert.equal(kit.store.taskForMessage('t','m1').result,'都做了：压缩包在这');assert.equal(kit.agent.jobs.length,1);
  kit.store.close();
});
test('a message from another chat is not handed to the running task: it runs on its own after it, in its own chat',async()=>{
  const handed=[];
  const kit=setup(async function*(job,n){
    yield started('th');job.steerable(async input=>{handed.push(input);});
    if(n===1)await until(()=>kit.store.taskForMessage('t','m2'));
    yield say('a',`答：${job.input.text}`);yield completed;
  });
  const {task,done}=await running(kit);
  await kit.service.receive(sent('m2','监听本群更新审片规范',{chat:'group',chatType:'group'}));
  assert.equal(kit.store.taskForMessage('t','m2').status,'QUEUED');await done;
  const scheduler=new Scheduler(kit.service);scheduler.tick();await until(()=>kit.store.taskForMessage('t','m2').status==='COMPLETED');await scheduler.stop();
  assert.deepEqual(handed,[],'nothing went into the first turn');
  assert.deepEqual(kit.agent.jobs.map(j=>j.user.chat),[kit.store.get(task.id).chat_id,'group']);
  assert.deepEqual(kit.agent.jobs.map(j=>j.user.chatType),[undefined,'group'],'the kind its message said; none for a chat this service has had no message from');
  kit.store.close();
});
test('a message that needs a resource lock the running task does not hold is not handed to it: it runs on its own, with the lock',async()=>{
  const saved=config.resources;config.resources=[{key:'printer',regex:/打印/}];
  try{
    const kit=setup(async function*(job){yield started('th');job.steerable(async()=>{});await until(()=>kit.store.taskForMessage('t','m2'));yield say('a','好');yield completed;});
    const {done}=await running(kit);
    await kit.service.receive(sent('m2','顺便打印合同'));assert.equal(kit.store.taskForMessage('t','m2').status,'QUEUED');
    await done;kit.store.close();
  }finally{config.resources=saved;}
});
test('a message sent while /new stops the running task is for the new conversation: it does not go into the stopped task, nor start before the conversation is replaced',async()=>{
  const kit=setup(async function*(job,n){
    yield started(n===1?'th-old':'th-new');job.steerable(async()=>{});
    // The stopped process takes a moment to go, so that /new is still waiting when the next message comes.
    if(n===1){await new Promise(r=>job.signal.addEventListener('abort',()=>setTimeout(r,300),{once:true}));throw aborted();}
    yield say('a','新对话里做好了');yield completed;
  });
  const {task}=await running(kit),scheduler=new Scheduler(kit.service);scheduler.start();
  const resetting=kit.service.receive(sent('x','/new'));
  await kit.service.receive(sent('m2','新对话的第一件'));assert.equal(kit.store.taskForMessage('t','m2').status,'QUEUED','not handed to the task being stopped');
  await resetting;await until(()=>kit.store.taskForMessage('t','m2').status==='COMPLETED');await scheduler.stop();
  assert.equal(kit.store.get(task.id).status,'INTERRUPTED');assert.equal(kit.agent.jobs[1].sessionId,null,'started in the new conversation');
  kit.store.close();
});
test('a message sent meanwhile whose attachment cannot be taken is refused at once, as a message of its own would be',async()=>{
  const downloader={downloadResource:async()=>{throw new InputRejected('附件超过 100 MB 接收上限，请拆分后分别发送');}};
  const kit=setup(async function*(job){yield started('th');job.steerable(async()=>{});await until(()=>kit.store.taskForMessage('t','m2')?.status==='FAILED');yield say('a','好');yield completed;},{downloader});
  const {done}=await running(kit);
  await kit.service.receive(sent('m2','',{attachments:[{kind:'file',key:'z',name:'大.zip'}]}));
  await until(()=>kit.store.taskForMessage('t','m2').status==='FAILED');assert.equal(kit.store.taskForMessage('t','m2').error,'附件超过 100 MB 接收上限，请拆分后分别发送');
  await done;kit.store.close();
});
test('a turn left because one of its events could not be recorded is still closed: the agent does not go on after the task has failed',async()=>{
  let closed=false;
  const kit=setup(async function*(){try{yield started('th');await new Promise(()=>{});}finally{closed=true;}});
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
