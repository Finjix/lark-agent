import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {Store} from '../dist/storage/database.js';
import {FrameDecoder,encodeFrame,FramedPipe,OutcomeUnknown} from '../dist/desktop/transport.js';
import {StreamState,applyPatch,interactionsOf} from '../dist/desktop/stream.js';
import {canSchedule,Scheduler} from '../dist/tasks/scheduler.js';
import {TaskService} from '../dist/tasks/service.js';
import {ownedArtifact,artifactLinks,outputFiles,Delivery} from '../dist/feishu/delivery.js';
import {cardJson,renderTask,limitText} from '../dist/feishu/cards.js';
import {EventEmitter} from 'node:events';
import {sanitizedFeishuError,FeishuClient} from '../dist/feishu/client.js';

function input(message='m1',user='u1',tenant='t1'){return {tenant_key:tenant,user_id:user,chat_id:'chat-'+user,source_message_id:message,instruction:'任务 '+message};}
function memory(){return new Store(':memory:');}
const broadcast=(id,change)=>({type:'broadcast',method:'thread-stream-state-changed',params:{hostId:'local',conversationId:id,change}});
const state=(turns,requests=[])=>({turnHistory:{kind:'canonical',history:{entitiesByKey:Object.fromEntries(turns.map(t=>['turn:'+t.turnId,t]))}},requests});
const turn=(id,marker,answer,status='inProgress')=>({turnId:id,params:{clientUserMessageId:marker},status,items:[{type:'userMessage',content:[{type:'text',text:marker}]},{type:'reasoning',content:'SECRET_INTERNAL_REASONING'},{type:'agentMessage',phase:'commentary',text:'公开进度'},{type:'agentMessage',phase:'final_answer',text:answer}]});

test('frames handle UTF-8 splits, coalesced messages, and reject oversized frames',()=>{
  const d=new FrameDecoder(1000),a=encodeFrame({text:'中文🙂'}),b=encodeFrame({id:2});
  assert.deepEqual(d.push(a.subarray(0,6)),[]);
  assert.deepEqual(d.push(Buffer.concat([a.subarray(6),b])),[{text:'中文🙂'},{id:2}]);
  const h=Buffer.alloc(4);h.writeUInt32LE(1001);assert.throws(()=>d.push(h),/length/);
});
test('SDK failures strip request objects and authentication data from thrown errors',()=>{
  const error=sanitizedFeishuError({message:'bad',config:{headers:{Authorization:'Bearer SECRET'}},response:{data:{code:400,msg:'field validation failed',error:{field_violations:[{field:'card',description:'required'}]}}}});
  assert.equal(error.code,400);assert(error.message.includes('card'));assert(!JSON.stringify(error).includes('SECRET'));assert.equal(error.config,undefined);
});
test('Feishu connection health reads the SDK status, so quiet reconnects and terminal failures are both reported correctly',()=>{
  const f=new FeishuClient({appId:'cli_offline_test',appSecret:'offline-test-placeholder'});
  assert.equal(f.state,'idle');assert.equal(f.connected,false);
  let state='connecting';f.ws={getConnectionStatus:()=>({state}),close(){}};
  assert.equal(f.connected,false);
  state='connected';assert.equal(f.connected,true);
  // The SDK logs a handshake failure and the later success at different levels; neither may latch the flag.
  f.logger.error('[ws]','ws connect failed');f.logger.debug('[ws]','reconnect success');assert.equal(f.connected,true);
  state='reconnecting';assert.equal(f.connected,false);
  state='failed';assert.equal(f.connected,false);assert.equal(f.state,'failed');
  f.close();assert.equal(f.state,'idle');assert.equal(f.started,false);
});
test('routing requires exact thread and turn, excludes reasoning, and handles already finished first turn',()=>{
  const s=new StreamState();s.receive(broadcast('thread-a',{type:'snapshot',revision:1,conversationState:state([turn('old','other','OLD','completed'),turn('new','[TASK:A]','RESULT-A','completed')])}));
  s.receive(broadcast('thread-b',{type:'snapshot',revision:1,conversationState:state([turn('b','[TASK:B]','RESULT-B')])}));
  assert.equal(s.view('thread-a',null,'[TASK:A]').turnId,'new');
  assert.equal(s.view('thread-a','new').answer,'RESULT-A');
  assert.equal(s.view('thread-a','b'),undefined);
  assert(!JSON.stringify(s.view('thread-a','new')).includes('SECRET_INTERNAL_REASONING'));
});
test('patch gaps resync; a bad batch is dropped instead of leaving half-applied state; duplicate patches are ignored',()=>{
  const s=new StreamState();let resync=0;s.on('resync',()=>resync++);
  s.receive(broadcast('a',{type:'snapshot',revision:3,conversationState:{value:'old'}}));
  s.receive(broadcast('a',{type:'patches',baseRevision:2,revision:4,patches:[]}));
  assert.equal(resync,1);
  s.receive(broadcast('a',{type:'patches',baseRevision:3,revision:4,patches:[{op:'replace',path:['value'],value:'changed'},{op:'remove',path:['missing','key']}]}));
  assert.equal(s.states.has('a'),false,'the half-changed state is not kept');assert.equal(resync,2,'and Desktop is asked again');
  s.receive(broadcast('a',{type:'snapshot',revision:3,conversationState:{value:'old'}}));
  s.receive(broadcast('a',{type:'patches',baseRevision:3,revision:4,patches:[{op:'replace',path:['value'],value:'new'}]}));
  s.receive(broadcast('a',{type:'patches',baseRevision:3,revision:4,patches:[{op:'replace',path:['value'],value:'duplicate'}]}));
  assert.equal(s.states.get('a').state.value,'new');
  assert.throws(()=>applyPatch({}, {op:'add',path:['__proto__','polluted'],value:true}));assert.equal({}.polluted,undefined);
});
test('approval routing preserves numeric wire request IDs and excludes another turn',()=>{
  const items=interactionsOf({requests:[{id:42,method:'item/commandExecution/requestApproval',params:{turnId:'a',reason:'需要授权'}},{id:43,method:'item/tool/requestUserInput',params:{turnId:'b'}}]},'a');
  assert.equal(items.length,1);assert.equal(items[0].id,'42');assert.equal(items[0].payload._requestId,42);
});
test('SQLite deduplicates within tenant, preserves recovery links and leaves a newer outbox generation pending',async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'lark-db-'));const file=path.join(dir,'test.sqlite');let s=new Store(file);
  const a=s.create(input());assert(a.created);assert.equal(s.create(input()).created,false);assert(s.create(input('m1','u1','t2')).created);
  s.transition(a.task.id,'STARTING',{thread_id:'thread',turn_id:'turn'});
  const old=s.due().find(x=>x.task_id===a.task.id);s.enqueue(old.id,a.task.id,'VIEW',{});s.ack(old.id,old.version);
  assert(s.due().some(x=>x.id===old.id));
  s.transition(a.task.id,'COMPLETED',{result:'完成'});s.close();s=new Store(file);
  assert.equal(s.get(a.task.id).turn_id,'turn');assert.equal(s.get(a.task.id).status,'COMPLETED');assert(s.due().some(x=>x.task_id===a.task.id));
  s.transition(a.task.id,'RUNNING');assert.equal(s.get(a.task.id).status,'COMPLETED');s.close();await fs.rm(dir,{recursive:true});
});
test('terminal transition and final outbox are atomic on database failure',()=>{
  const s=memory(),t=s.create(input()).task;
  s.db.exec("CREATE TRIGGER fail_outbox BEFORE UPDATE ON delivery_outbox BEGIN SELECT RAISE(ABORT,'disk failure'); END;");
  assert.throws(()=>s.transition(t.id,'COMPLETED',{result:'ok'}));assert.equal(s.get(t.id).status,'QUEUED');s.close();
});
test('session and resource conflicts still block UNKNOWN and waiting tasks without a global task cap',()=>{
  const s=memory(),a=s.create(input('a')).task,b=s.create(input('b')).task,c=s.create(input('c')).task,d=s.create(input('d','u2')).task;
  a.status='UNKNOWN';b.status='WAITING_APPROVAL';
  assert.equal(canSchedule(c,[a,b]),false);assert.equal(canSchedule(d,[a,b]),true);
  c.thread_id='same';d.thread_id='same';assert.equal(canSchedule(c,[d]),false);
  c.thread_id=null;c.resources='["shared"]';d.resources='["shared"]';assert.equal(canSchedule(c,[d]),false);s.close();
});
test('equal-timestamp messages serialize per user and a blocked user does not block other users',async()=>{
  const s=memory(),d=new EventEmitter(),releases=[];d.ready=true;d.startTask=task=>new Promise(resolve=>releases.push(()=>resolve({threadId:task.id,workspace:'test'})));d.follow=async()=>{};
  const tasks=['A','A','A','B','C'].map((user,i)=>s.create(input('fifo-'+i,user)).task);
  for(const t of tasks)s.update(t.id,{created_at:1000});
  assert.deepEqual(s.list('QUEUED').map(t=>t.id),tasks.map(t=>t.id));
  const scheduler=new Scheduler(new TaskService(s,d));scheduler.tick();
  assert.deepEqual(s.active().map(t=>t.id),[tasks[0].id,tasks[3].id,tasks[4].id]);
  await new Promise(r=>setImmediate(r));for(const release of releases)release();await scheduler.stop();s.close();
});

test('scheduler launches every independent user without a numeric cap and stop awaits all submissions',async()=>{
  const s=memory(),releases=[],started=[];
  const tasks=Array.from({length:24},(_,i)=>s.create(input(`parallel-${i}`,`user-${i}`)).task);
  const next=s.create(input('same-user-next','user-0')).task;
  const service={store:s,desktop:{ready:true},start:task=>{started.push(task.id);return new Promise(resolve=>releases.push(resolve));}};
  const scheduler=new Scheduler(service);scheduler.tick();await new Promise(resolve=>setImmediate(resolve));
  assert.equal(started.length,24);assert.equal(s.active().length,24);assert.equal(s.get(next.id).status,'QUEUED');
  scheduler.tick();await new Promise(resolve=>setImmediate(resolve));assert.equal(started.length,24);
  let stopped=false;const stopping=scheduler.stop().then(()=>{stopped=true;});await new Promise(resolve=>setImmediate(resolve));assert.equal(stopped,false);
  for(const release of releases)release();await stopping;assert.equal(stopped,true);
  s.transition(tasks[0].id,'COMPLETED');scheduler.tick();assert.equal(s.get(next.id).status,'QUEUED');s.close();
});
test('cross-user and cross-tenant commands cannot cancel another task',async()=>{
  const s=memory(),desktop=new EventEmitter(),service=new TaskService(s,desktop),task=s.create(input()).task;
  const message={tenant:'t1',user:'u2',chat:'c',message:'command',text:`/cancel ${task.id}`};
  // Invoke command authorization itself, independently of the configured allowlist.
  await service.command(message);assert.equal(s.get(task.id).status,'QUEUED');
  const out=s.due().find(x=>x.kind==='NOTICE');assert(JSON.parse(out.payload).text.includes('无权'));
  await service.command({...message,tenant:'t2',user:'u1',message:'command2'});assert.equal(s.get(task.id).status,'QUEUED');s.close();
});
test('uncertain creation reserves its slot and is never automatically submitted again',async()=>{
  const s=memory(),d=new EventEmitter();let calls=0;d.startTask=async()=>{calls++;throw new OutcomeUnknown('lost response');};const service=new TaskService(s,d),task=s.create(input()).task;
  s.transition(task.id,'STARTING');await service.start(s.get(task.id));assert.equal(s.get(task.id).status,'UNKNOWN');assert.equal(s.active().length,1);assert.equal(calls,1);s.close();
});
test('file collection rejects traversal and symlinks, accepts spaces and unicode',async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'lark-files-')),workspace=path.join(dir,'任务 空格');await fs.mkdir(workspace);
  const file=path.join(workspace,'报告.txt'),outside=path.join(dir,'secret.txt');await fs.writeFile(file,'safe');await fs.writeFile(outside,'secret');
  assert.equal((await ownedArtifact(workspace,file)).name,'报告.txt');await assert.rejects(ownedArtifact(workspace,outside));
  const linked=path.join(workspace,'linked');try{await fs.symlink(dir,linked,'junction');await assert.rejects(ownedArtifact(workspace,path.join(linked,'secret.txt')));}catch(e){if(e.code!=='EPERM')throw e;}
  assert.deepEqual(artifactLinks('[报告](<D:/测试 空格/报告.txt>)'),['D:/测试 空格/报告.txt']);await fs.rm(dir,{recursive:true});
});
test('card output respects byte limits for multibyte text and contains no unrelated task content',()=>{
  const s=memory(),task=s.create(input()).task;task.result='汉🙂'.repeat(12000);task.status='COMPLETED';
  assert(Buffer.byteLength(limitText(task.result,1000))<=1000);const content=renderTask(task);assert(Buffer.byteLength(content)<=24000);
  assert.equal(cardJson(task,content).config.streaming_mode,false);s.close();
});

test('streamed replies contain public commentary and answer without task chrome or tool status',async()=>{
  const s=memory(),task=s.create(input()).task,desktop=Object.assign(new EventEmitter(),{stream:new StreamState(),unfollow:()=>{}});
  const service=new TaskService(s,desktop),calls=[];
  const fake={connected:true,raw:async(method,url,data)=>{calls.push({method,url,data});return url.endsWith('/cards')?{card_id:'card'}:{};},
    reply:async(...args)=>{calls.push({reply:args});return {message_id:'message'};}};
  const delivery=new Delivery(s,fake);
  await delivery.tick();assert.equal(calls.length,0); // No queued status bubble.
  s.transition(task.id,'STARTING',{thread_id:'thread',turn_id:'turn'});
  const publish=async(revision,answer,status='inProgress')=>{
    const item=turn('turn','',answer,status);item.items.push({type:'commandExecution',status:'inProgress',output:'PRIVATE COMMAND OUTPUT'});
    desktop.stream.receive(broadcast('thread',{type:'snapshot',revision,conversationState:state([item])}));
    service.onState('thread');await delivery.tick();
  };
  await publish(1,'正在');await publish(2,'正在输出');await publish(3,'正在输出完整回复','completed');
  const activeCards=calls.filter(c=>c.method==='PUT'&&c.data.card&&JSON.parse(c.data.card.data).config.streaming_mode).map(c=>JSON.parse(c.data.card.data));
  const bodies=[...activeCards.map(c=>c.body.elements[0].content),...calls.filter(c=>c.url?.endsWith('/elements/task_content/content')).map(c=>c.data.content)];
  assert.deepEqual(bodies,['公开进度\n\n正在','公开进度\n\n正在输出','公开进度\n\n正在输出完整回复']);
  assert.match(activeCards[0].header.title.content,/^番薯搬砖中\.\.\.\.\.\. \d+s$/);assert.equal(activeCards[0].header.template,'green');
  const final=calls.filter(c=>c.method==='PUT'&&c.url==='/open-apis/cardkit/v1/cards/card').at(-1);
  const card=JSON.parse(final.data.card.data);assert.equal(card.header,undefined);assert.equal(card.config.streaming_mode,false);
  assert.equal(card.body.elements[0].content,bodies.at(-1));
  const serialized=JSON.stringify(calls);
  for(const excluded of ['耗时','查询：','状态：','**回复**','**进度**','commandExecution','SECRET_INTERNAL_REASONING','PRIVATE COMMAND OUTPUT'])assert(!serialized.includes(excluded),excluded);
  assert(!serialized.includes(`任务 ${task.id}`));
  await new Promise(resolve=>setImmediate(resolve));await delivery.stop();s.close();
});

test('minimal reply still exposes approval commands, errors, and cancellation',()=>{
  const s=memory(),task=s.create(input()).task;
  assert.equal(renderTask(task),'');
  task.status='WAITING_APPROVAL';
  const request={id:'req',kind:'command',summary:'允许执行此命令？',payload:{}};
  const content=renderTask(task,[request]);assert(content.includes(`/approve ${task.id} req`));assert(content.includes(`/deny ${task.id} req`));
  assert(renderTask(task,[{...request,kind:'input'}]).includes(`/answer ${task.id} req`));
  task.status='FAILED';task.error='连接失败';assert.equal(renderTask(task),'连接失败\n\n执行失败');
  task.status='CANCELLED';task.error='';assert.equal(renderTask(task),'已取消');s.close();
});

test('working card shows only the requested status and seconds, and drops it when execution stops',()=>{
  const s=memory(),task=s.create(input()).task;task.status='RUNNING';task.started_at=1000;
  assert.equal(cardJson(task,'',true,13500).header.title.content,'番薯搬砖中...... 12s');
  assert.equal(cardJson(task,'',true,500).header.title.content,'番薯搬砖中...... 0s');
  task.progress='正在查看图片';assert.equal(renderTask(task),'正在查看图片');
  for(const status of ['WAITING_APPROVAL','UNKNOWN','COMPLETED','FAILED','CANCELLED','INTERRUPTED']){
    task.status=status;assert.equal(cardJson(task,renderTask(task)).header,undefined);
  }
  s.close();
});

test('timer refreshes without new Desktop output, reuses the message, and respects delivery retry backoff',async()=>{
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
test('outbox retries delivery without calling Desktop and serializes card sequences',async()=>{
  const s=memory(),task=s.create(input()).task;s.transition(task.id,'COMPLETED',{result:'ok'});let fail=true;const sequences=[];
  const fake={connected:true,raw:async(method,url,data)=>{if(fail){fail=false;throw Error('network down');}if(url.endsWith('/cards'))return {card_id:'card'};sequences.push(data.sequence);return {};},reply:async()=>({message_id:'reply'})};
  const delivery=new Delivery(s,fake);await delivery.tick();assert.equal(s.get(task.id).status,'COMPLETED');
  s.db.prepare('UPDATE delivery_outbox SET next_attempt_at=0').run();await delivery.tick();
  assert.equal(s.due().length,0);assert.equal(new Set(sequences).size,sequences.length);assert.deepEqual(sequences,[...sequences].sort((a,b)=>a-b));await delivery.stop();s.close();
});
test('a link to a file outside the delivery folder is ignored without a notice and never uploaded',async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'lark-rejected-')),workspace=path.join(dir,'workspace');await fs.mkdir(workspace);
  const outside=path.join(dir,'outside.txt');await fs.writeFile(outside,'never upload');const s=memory(),task=s.create(input()).task;
  s.transition(task.id,'COMPLETED',{workspace_path:workspace,result:`[file](<${outside}>)`});let uploaded=false;
  const f={connected:true,raw:async(method,url)=>url.endsWith('/cards')?{card_id:'card'}:{},reply:async()=>({message_id:'message'}),upload:async()=>{uploaded=true;}};
  const delivery=new Delivery(s,f);await delivery.tick();assert.equal(s.db.prepare("SELECT count(*) c FROM delivery_outbox WHERE kind='NOTICE'").get().c,0,'no notice');assert.equal(s.db.prepare('SELECT count(*) c FROM task_artifacts').get().c,0);assert.equal(uploaded,false);
  await delivery.stop();s.close();await fs.rm(dir,{recursive:true});
});
test('whatever Codex leaves in the task output folder is delivered even when the reply does not link it',async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'lark-scan-')),workspace=path.join(dir,'output','20261016_170000');
  await fs.mkdir(path.join(workspace,'sub'),{recursive:true});
  await fs.writeFile(path.join(workspace,'report.txt'),'r');await fs.writeFile(path.join(workspace,'sub','data.csv'),'d');await fs.writeFile(path.join(workspace,'chart.png'),'p');
  await fs.writeFile(path.join(workspace,'.scratch'),'x');await fs.writeFile(path.join(dir,'elsewhere.txt'),'never');
  const s=memory(),task=s.create(input()).task;s.transition(task.id,'COMPLETED',{workspace_path:workspace,result:`done, see [report](<${path.join(workspace,'report.txt')}>)`});
  const f={connected:true,raw:async(method,url)=>url.endsWith('/cards')?{card_id:'card'}:{},reply:async()=>({message_id:'message'}),upload:async()=>({file_key:'file'}),uploadImage:async()=>({image_key:'image'})};
  const delivery=new Delivery(s,f);await delivery.tick();
  const rows=s.db.prepare('SELECT name,kind FROM task_artifacts WHERE task_id=? ORDER BY name').all(task.id).map(r=>`${r.name}:${r.kind}`);
  assert.deepEqual(rows,['chart.png:image','data.csv:file','report.txt:file'],'linked and unlinked files once each; dotfiles and files outside the folder are not sent');
  await delivery.stop();s.close();await fs.rm(dir,{recursive:true});
  const many=await fs.mkdtemp(path.join(os.tmpdir(),'lark-scan-many-'));for(let n=0;n<25;n++)await fs.writeFile(path.join(many,`f${String(n).padStart(2,'0')}.txt`),'x');
  assert.equal((await outputFiles(many)).length,10,'capped');await fs.rm(many,{recursive:true});
});

test('a task owns only the files that appeared in output/ while it ran',async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'lark-window-'));await fs.writeFile(path.join(dir,'report.txt'),'r');
  const now=Date.now(),names=async window=>(await outputFiles(dir,window)).map(f=>path.basename(f));
  assert.deepEqual(await names({from:now-5000,to:now+5000}),['report.txt'],'created during the task');
  assert.deepEqual(await names({from:now-60000,to:now-30000}),[],'a file of a later task is not this task\'s');
  assert.deepEqual(await names({from:now+30000,to:now+60000}),[],'a file from before the task started is not sent again');
  // A copy keeps the old modification time but is a new file, so asking Codex to resend something still works.
  const old=path.join(dir,'old.txt');await fs.writeFile(old,'o');const past=new Date(now-86400000);await fs.utimes(old,past,past);
  const copy=path.join(dir,'copy.txt');await fs.copyFile(old,copy);
  assert((await names({from:now-5000,to:now+5000})).includes('copy.txt'),'a copied file counts by when it was created here');
  await fs.rm(dir,{recursive:true});
});

test('reply links: parentheses inside a path are kept, other markdown is not mistaken for a file',()=>{
  assert.deepEqual(artifactLinks('[a](<D:/x/in/report (2).pdf>) [b](D:/x/out/report (2).pdf) [c](D:\\x\\a.txt:12)'),['D:/x/in/report (2).pdf','D:/x/out/report (2).pdf','D:\\x\\a.txt']);
  assert.deepEqual(artifactLinks('[site](https://example.com/a) [rel](docs/a.md) [anchor](#top) [open](<D:/x/unclosed.txt) next line)'),[]);
  assert.deepEqual(artifactLinks('see [a](D:/x/a.txt) and (parenthetical) then [b](D:/x/b (1).txt)'),['D:/x/a.txt','D:/x/b (1).txt']);
});
test('a delivered file with parentheses in its name is sent, a link to a missing file inside the folder is still reported, links elsewhere are not',async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'lark-links-')),workspace=path.join(dir,'outputs'),input_=path.join(dir,'input');await fs.mkdir(workspace);await fs.mkdir(input_);
  const named=path.join(workspace,'report (2).pdf'),mine=path.join(input_,'photo (2).png');await fs.writeFile(named,'r');await fs.writeFile(mine,'m');
  const s=memory(),task=s.create(input()).task;
  s.transition(task.id,'COMPLETED',{workspace_path:workspace,result:`[报告](<${named}>) [你的图](<${mine}>) [站内](/docs/intro) [丢失](<${path.join(workspace,'missing.txt')}>)`});
  const f={connected:true,raw:async(method,url)=>url.endsWith('/cards')?{card_id:'card'}:{},reply:async()=>({message_id:'message'}),upload:async()=>({file_key:'file'}),uploadImage:async()=>({image_key:'image'})};
  const delivery=new Delivery(s,f);await delivery.tick();
  assert.deepEqual(s.db.prepare('SELECT name FROM task_artifacts WHERE task_id=?').all(task.id).map(r=>r.name),['report (2).pdf']);
  const notices=s.db.prepare("SELECT payload FROM delivery_outbox WHERE kind='NOTICE'").all().map(r=>JSON.parse(r.payload).text);
  assert.equal(notices.length,1);assert(notices[0].includes('missing.txt')&&notices[0].includes('附件未发送'));
  await delivery.stop();s.close();await fs.rm(dir,{recursive:true});
});

test('a slow upload does not hold back the card updates of other tasks',async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'lark-slow-')),workspace=path.join(dir,'outputs');await fs.mkdir(workspace);
  const big=path.join(workspace,'big.bin');await fs.writeFile(big,'x');
  const s=memory(),a=s.create(input('a','u1')).task,b=s.create(input('b','u2')).task;
  s.transition(a.id,'COMPLETED',{workspace_path:workspace,result:`[大文件](<${big}>)`});s.transition(b.id,'RUNNING',{started_at:Date.now(),progress:'还在做'});
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
test('a task waiting for the user gets its card refreshed without the stale working header',async()=>{
  const s=memory(),task=s.create(input()).task;s.transition(task.id,'RUNNING',{started_at:Date.now()});
  const calls=[],f={connected:true,raw:async(method,url,data)=>{calls.push({method,url,data});return url.endsWith('/cards')?{card_id:'card'}:{};},reply:async()=>({message_id:'m'})};
  const delivery=new Delivery(s,f);await delivery.tick();
  s.setRequests(task.id,[{id:'r1',kind:'command',summary:'允许？',payload:{}}]);s.transition(task.id,'WAITING_APPROVAL');
  calls.length=0;await delivery.tick();
  const card=calls.filter(c=>c.method==='PUT'&&c.data.card).map(c=>JSON.parse(c.data.card.data)).at(-1);
  assert(card,'the whole card is replaced, not just its text');assert.equal(card.header,undefined);assert(card.body.elements[0].content.includes('/approve'));
  await delivery.stop();s.close();
});
test('screenshots in the hidden folder are not sent unless the reply links them',async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'lark-shots-'));await fs.mkdir(path.join(dir,'.screenshots'));
  await fs.writeFile(path.join(dir,'.screenshots','look.png'),'p');await fs.writeFile(path.join(dir,'report.txt'),'r');
  assert.deepEqual((await outputFiles(dir)).map(f=>path.basename(f)),['report.txt']);await fs.rm(dir,{recursive:true});
});
test('a task Desktop cannot be asked about is marked UNKNOWN once, not rewritten on every pass',async()=>{
  const s=memory(),task=s.create(input()).task;s.transition(task.id,'RUNNING',{thread_id:'th',turn_id:'tu'});
  const service=new TaskService(s,Object.assign(new EventEmitter(),{follow:async()=>{throw new Error('no-client-found');}}));
  await service.reconcile();const after=s.db.prepare('SELECT count(*) c FROM task_events WHERE task_id=?').get(task.id).c;
  await service.reconcile();await service.reconcile();
  assert.equal(s.get(task.id).status,'UNKNOWN');assert.equal(s.db.prepare('SELECT count(*) c FROM task_events WHERE task_id=?').get(task.id).c,after);s.close();
});
