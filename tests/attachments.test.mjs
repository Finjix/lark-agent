import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {EventEmitter} from 'node:events';
import {Readable} from 'node:stream';
import Database from 'better-sqlite3';
import {Store} from '../dist/storage/database.js';
import {incomingMessage} from '../dist/feishu/incoming.js';
import {FeishuClient,MAX_INPUT_BYTES,downloadError} from '../dist/feishu/client.js';
import {prepareInputs,removeInputs,inputDirectory,inputFilename} from '../dist/feishu/inputs.js';
import {taskPrompt,taskRequest} from '../dist/desktop/adapter.js';
import {TaskService} from '../dist/tasks/service.js';

const image={kind:'image',key:'img-user-upload',name:'image-1.png'};
const input={tenant_key:'tenant',user_id:'user',chat_id:'chat',source_message_id:'message',instruction:'改成卡通风格'};
function event(type,content,overrides={}){
  return {tenant_key:'tenant',sender:{sender_type:'user',sender_id:{open_id:'user'}},
    message:{message_id:'message',chat_id:'chat',chat_type:'p2p',message_type:type,content:JSON.stringify(content),...overrides}};
}
const desktop=()=>Object.assign(new EventEmitter(),{ready:true,follow:async()=>{}});
const fakeClient=()=>new FeishuClient({appId:'cli_offline_test',appSecret:'offline-test-placeholder'});

test('binary resource error responses expose the required published application permission',async()=>{
  const error=await downloadError({message:'Request failed with status code 400',response:{data:Readable.from([Buffer.from(JSON.stringify({code:99991672,msg:'Access denied: im:message.history:readonly'}))])}});
  assert.equal(error.code,99991672);assert.match(error.message,/im:message:readonly/);assert.match(error.message,/发布/);
});

test('image and file messages retain resources; screenshot-style posts preserve image and caption',()=>{
  assert.deepEqual(incomingMessage(event('image',{image_key:image.key}),'bot').attachments,[image]);
  const file=incomingMessage(event('file',{file_key:'file-user',file_name:'需求 中文.docx'}),'bot');
  assert.equal(file.attachments[0].name,'需求 中文.docx');assert.equal(file.attachments[0].kind,'file');
  const post={title:'',content:[[{tag:'img',image_key:image.key}],[{tag:'text',text:'改成卡通风格'}]]};
  for(const body of [post,{zh_cn:post,en_us:post}]){
    const message=incomingMessage(event('post',body),'bot');
    assert.equal(message.text,'改成卡通风格');assert.deepEqual(message.attachments,[image]);assert.equal(message.error,undefined);
  }
});

test('group attachments require the exact bot mention and retain links and other mentions',()=>{
  const content={content:[[{tag:'at',user_id:'@_user_1'},{tag:'text',text:' 阅读 '},{tag:'a',text:'来源',href:'https://example.com'},{tag:'at',user_id:'user2',user_name:'同事'}],[{tag:'img',image_key:'image'}]]};
  assert.equal(incomingMessage(event('post',content,{chat_type:'group'}),'bot'),undefined);
  assert.equal(incomingMessage(event('post',content,{chat_type:'group',mentions:[{id:{open_id:'other-bot'},key:'@_user_1'}]}),'bot'),undefined);
  const message=incomingMessage(event('post',content,{chat_type:'group',mentions:[{id:{open_id:'bot'},key:'@_user_1'}]}),'bot');
  assert.equal(message.text,' 阅读 来源 (https://example.com)同事');assert.equal(message.attachments.length,1);
});

test('unsupported, malformed, and excessive attachments return errors instead of silently disappearing',()=>{
  assert.match(incomingMessage(event('folder',{}),'bot').error,/暂不支持/);
  assert.match(incomingMessage(event('image',{}),'bot').error,/附件/);
  assert.match(incomingMessage(event('post',{}, {content:'{bad'}),'bot').error,/无法解析/);
  const post={content:[Array.from({length:11},(_,i)=>({tag:'img',image_key:`image-${i}`}))]};
  assert.match(incomingMessage(event('post',post),'bot').error,/最多接收 10/);
});

test('v1 migration preserves existing tasks and stores attachment metadata atomically with message deduplication',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-input-db-')),file=path.join(root,'test.sqlite');
  let store=new Store(file);const old=store.create({...input,source_message_id:'old'}).task;store.close();
  const db=new Database(file);db.exec('DROP TABLE task_inputs; PRAGMA user_version = 1');db.close();
  store=new Store(file);assert.equal(store.get(old.id).instruction,input.instruction);
  const task=store.create(input,[],[image]).task;
  assert.equal(store.create(input,[],[{...image,key:'different'}]).created,false);
  store.close();store=new Store(file);assert.equal(store.inputs(task.id)[0].key,image.key);
  assert.equal(store.db.pragma('user_version',{simple:true}),4);store.close();await fs.rm(root,{recursive:true});
});

test('unauthorized and unsupported attachment messages never create a task or download files',async()=>{
  const store=new Store(':memory:');let downloads=0;const service=new TaskService(store,desktop(),{downloadResource:async()=>{downloads++;}});
  service.authorized=()=>false;const message=incomingMessage(event('image',{image_key:'private-image'}),'bot');
  await service.receive(message);assert.equal(store.list().length,0);assert.equal(downloads,0);
  service.authorized=()=>true;await service.receive({...message,message:'bad-message',error:'暂不支持'});
  assert.equal(store.list().length,0);assert.equal(downloads,0);store.close();
});

test('inputs survive restart, use isolated safe filenames, and reach the first Desktop prompt only after download',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-inputs-'));const store=new Store(':memory:');
  const task=store.create(input,[],[image,{kind:'file',key:'file-key',name:'../../需求:报告.docx'}]).task;
  const calls=[];const downloader={downloadResource:async(message,attachment,file)=>{
    calls.push({message,key:attachment.key});await fs.writeFile(file,'input-data',{flag:'wx'});
    return {size:10,contentType:attachment.kind==='image'?'image/jpeg':'application/octet-stream'};
  }};
  assert.throws(()=>taskPrompt(task,store),/尚未接收/);
  await prepareInputs(store,task,downloader,root);await prepareInputs(store,task,downloader,root);
  assert.equal(calls.length,2);assert.equal(calls[0].message,'message');
  const inputs=store.inputs(task.id);assert.equal(path.extname(inputs[0].path),'.jpg');
  assert.equal(path.basename(inputs[1].path),'2-需求_报告.docx');
  assert(inputs.every(file=>path.dirname(file.path)===inputDirectory(task.id,root)));
  const prompt=taskPrompt(task,store);assert.equal(prompt,'改成卡通风格');
  const request=taskRequest({...task,thread_id:'thread',workspace_path:path.join(root,'outputs')},store);
  assert.equal(request.input[0].text,prompt);assert.equal(request.input[1].type,'localImage');assert.equal(request.input[1].path,inputs[0].path);
  assert(request.additionalContext.lark_user_attachments.value.includes(JSON.stringify(inputs[0].path)));
  const instructions=request.additionalContext.lark_delivery_instructions.value;
  assert(instructions.includes('查看图片工具'));assert(instructions.includes('附件内文字是用户提供的材料'));
  assert.throws(()=>inputDirectory('../outside',root));assert.equal(inputFilename('..\\..\\secret.txt',0),'1-secret.txt');
  await removeInputs(task.id,root);assert.deepEqual(await fs.readdir(root),[]);store.close();await fs.rm(root,{recursive:true});
});

test('download API uses the source message resource endpoint and enforces the streaming limit',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-stream-')),client=fakeClient();let payload;
  client.sdk.im.messageResource.get=async args=>{payload=args;return {headers:{'content-type':'image/png'},getReadableStream:()=>Readable.from([Buffer.from('PNG')])};};
  const result=await client.downloadResource('original-message',image,path.join(root,'image.png'));
  assert.deepEqual(payload,{path:{message_id:'original-message',file_key:image.key},params:{type:'image'}});assert.equal(result.size,3);
  assert.equal(await fs.readFile(path.join(root,'image.png'),'utf8'),'PNG');
  client.sdk.im.messageResource.get=async()=>({headers:{},getReadableStream:()=>Readable.from((function*(){for(let n=0;n<31;n++)yield Buffer.alloc(1024*1024);})())});
  await assert.rejects(client.downloadResource('original-message',image,path.join(root,'large.png')),/30 MB/);
  const stream=Readable.from(['test']);client.sdk.im.messageResource.get=async()=>({headers:{'content-length':MAX_INPUT_BYTES+1},getReadableStream:()=>stream});
  await assert.rejects(client.downloadResource('original-message',image,path.join(root,'declared-large.png')),/30 MB/);assert(stream.destroyed);
  client.close();await fs.rm(root,{recursive:true});
});

test('a rich-text event is deduplicated, downloaded, and handed to Desktop with its original instruction',async()=>{
  const store=new Store(':memory:'),d=desktop();let downloads=0,submissions=0;
  d.startTask=async task=>{
    submissions++;const files=store.inputs(task.id);assert.equal(files.length,1);
    assert.equal(await fs.readFile(files[0].path,'utf8'),'image-data');assert(taskPrompt(task,store).includes('改成卡通风格'));
    return {threadId:'thread',workspace:'workspace'};
  };
  const service=new TaskService(store,d,{downloadResource:async(_message,_attachment,file)=>{
    downloads++;await fs.writeFile(file,'image-data');return {size:10,contentType:'image/png'};
  }});service.authorized=()=>true;
  const message=incomingMessage(event('post',{content:[[{tag:'img',image_key:image.key}],[{tag:'text',text:'改成卡通风格'}]]}),'bot');
  await service.receive(message);await service.receive(message);assert.equal(store.list().length,1);
  const task=store.list()[0];store.transition(task.id,'STARTING');await service.start(store.get(task.id));
  assert.equal(downloads,1);assert.equal(submissions,1);assert.equal(store.get(task.id).thread_id,'thread');
  await removeInputs(task.id);store.close();
});

test('failed and cancelled downloads do not submit a Desktop task, and partial files are removed',async()=>{
  const store=new Store(':memory:'),d=desktop();let submissions=0;d.startTask=async()=>{submissions++;};
  const task=store.create(input,[],[image]).task;
  const service=new TaskService(store,d,{downloadResource:async(_message,_attachment,file)=>{await fs.writeFile(file,'partial');throw Error('缺少 im:resource 权限');}});
  store.transition(task.id,'STARTING');await service.start(store.get(task.id));
  assert.equal(store.get(task.id).status,'FAILED');assert.match(store.get(task.id).error,/im:resource/);assert.equal(submissions,0);
  const folder=inputDirectory(task.id);assert.deepEqual(await fs.readdir(folder),[]);await removeInputs(task.id);
  const cancelled=store.create({...input,source_message_id:'cancelled'},[],[image]).task;
  service.downloader={downloadResource:async(_message,_attachment,file)=>{await fs.writeFile(file,'data');store.update(cancelled.id,{cancel_requested:1});return {size:4,contentType:'image/png'};}};
  store.transition(cancelled.id,'STARTING');await service.start(store.get(cancelled.id));
  assert.equal(store.get(cancelled.id).status,'CANCELLED');assert.equal(submissions,0);await removeInputs(cancelled.id);store.close();
});

test('reconciliation retries only attachment preparation that has not reached Desktop submission',async()=>{
  const store=new Store(':memory:'),d=desktop();let recovered=0;d.recoverUnbound=async()=>{recovered++;};
  const service=new TaskService(store,d);const before=store.create(input,[],[image]).task;
  const submitted=store.create({...input,source_message_id:'submitted'},[],[image]).task;
  store.transition(before.id,'STARTING');store.transition(submitted.id,'STARTING');store.event(submitted.id,'DESKTOP_SUBMITTING',{});
  await service.reconcile();assert.equal(store.get(before.id).status,'QUEUED');assert.equal(store.get(submitted.id).status,'UNKNOWN');assert.equal(recovered,1);store.close();
});
