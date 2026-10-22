import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {EventEmitter} from 'node:events';
import {Readable} from 'node:stream';
import {Store} from '../dist/storage/database.js';
import {incomingMessage,FOLDER_HINT} from '../dist/feishu/incoming.js';
import {FeishuClient,MAX_INPUT_BYTES,MAX_IMAGE_BYTES,downloadError} from '../dist/feishu/client.js';
import {config} from '../dist/config.js';
import {prepareInputs,removeInputs,inputFilename,sessionInputs} from '../dist/feishu/inputs.js';
import {codexInput} from '../dist/codex/input.js';
import {TaskService} from '../dist/tasks/service.js';

const image={kind:'image',key:'img-user-upload',name:'image-1.png'};
const input={tenant_key:'tenant',user_id:'user',chat_id:'chat',source_message_id:'message',instruction:'改成卡通风格'};
function event(type,content,overrides={}){
  return {tenant_key:'tenant',sender:{sender_type:'user',sender_id:{open_id:'user'}},
    message:{message_id:'message',chat_id:'chat',chat_type:'p2p',message_type:type,content:JSON.stringify(content),...overrides}};
}
/** A Codex that records what it is given and answers at once. */
const codex=()=>Object.assign(new EventEmitter(),{ready:true,jobs:[],run:async function*(job){this.jobs.push(job);yield {type:'thread.started',thread_id:'thread'};yield {type:'turn.completed',usage:{}};}});
const fakeClient=()=>new FeishuClient({appId:'cli_offline_test',appSecret:'offline-test-placeholder'});

test('binary resource error responses expose the required published application permission',async()=>{
  const error=await downloadError({message:'Request failed with status code 400',response:{data:Readable.from([Buffer.from(JSON.stringify({code:99991672,msg:'Access denied: im:message.history:readonly'}))])}});
  assert.equal(error.code,99991672);assert.match(error.message,/im:message:readonly/);assert.match(error.message,/发布/);
});

test('text sent together with a file arrives as a post whose attachments sit in a top-level files array',()=>{
  // Shape taken from a real message: the file is not one of the content nodes.
  const body={title:'',content:[[{tag:'text',text:'这是什么',style:[]}]],content_v2:[[{tag:'text',text:'这是什么',style:[]}]],
    files:[{file_key:'file_v3_real',file_name:'新建 文本文档.txt',is_folder:false}]};
  const message=incomingMessage(event('post',body),'bot');
  assert.equal(message.text,'这是什么');assert.equal(message.error,undefined);
  assert.deepEqual(message.attachments,[{kind:'file',key:'file_v3_real',name:'新建 文本文档.txt'}]);
  assert.deepEqual(incomingMessage(event('post',{zh_cn:body}),'bot').attachments,message.attachments,'also inside a locale wrapper');
  const folder=incomingMessage(event('post',{...body,files:[{file_key:'dir',file_name:'资料',is_folder:true}]}),'bot');
  assert.equal(folder.error,FOLDER_HINT,'a folder sent with text is asked for as a zip');
  const mixed=incomingMessage(event('post',{...body,images:[{image_key:'img_1'}]}),'bot');
  assert.deepEqual(mixed.attachments.map(a=>a.kind),['file','image']);
  assert.equal(incomingMessage(event('post',{title:'',content:[[{tag:'text',text:'只有文字'}]]}),'bot').attachments.length,0);
});

test('a video sent on its own is a file attachment; a folder is asked for as a zip',()=>{
  // Shapes taken from real messages (2026-10-21).
  const video=incomingMessage(event('media',{file_key:'file_v3_video',file_name:'P01.mp4',image_key:'img_v3_cover',duration:22650}),'bot');
  assert.equal(video.error,undefined);assert.deepEqual(video.attachments,[{kind:'file',key:'file_v3_video',name:'P01.mp4'}]);
  assert.equal(incomingMessage(event('media',{file_key:'k'}),'bot').attachments[0].name,'video.mp4');
  const folder=incomingMessage(event('folder',{file_key:'file_v3_folder',file_name:'新建文件夹'}),'bot');
  assert.equal(folder.error,FOLDER_HINT);assert.match(FOLDER_HINT,/zip/);
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
  assert.match(incomingMessage(event('sticker',{}),'bot').error,/暂不支持/);
  assert.match(incomingMessage(event('image',{}),'bot').error,/附件/);
  assert.match(incomingMessage(event('post',{}, {content:'{bad'}),'bot').error,/无法解析/);
  const post={content:[Array.from({length:11},(_,i)=>({tag:'img',image_key:`image-${i}`}))]};
  assert.match(incomingMessage(event('post',post),'bot').error,/最多接收 10/);
});

test('attachment metadata is stored atomically with the task, survives a restart, and a repeated message is deduplicated',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-input-db-')),file=path.join(root,'test.sqlite');
  let store=new Store(file);const task=store.create(input,[],[image]).task;
  assert.equal(store.create(input,[],[{...image,key:'different'}]).created,false);
  store.close();store=new Store(file);assert.deepEqual(store.inputs(task.id).map(i=>[i.key,i.name,i.path]),[[image.key,image.name,null]],'one attachment, as first recorded');
  store.close();await fs.rm(root,{recursive:true});
});

test('unauthorized and unsupported attachment messages never create a task or download files',async()=>{
  const store=new Store(':memory:');let downloads=0;const service=new TaskService(store,codex(),{downloadResource:async()=>{downloads++;}});
  service.authorized=()=>false;const message=incomingMessage(event('image',{image_key:'private-image'}),'bot');
  await service.receive(message);assert.equal(store.list().length,0);assert.equal(downloads,0);
  service.authorized=()=>true;await service.receive({...message,message:'bad-message',error:'暂不支持'});
  assert.equal(store.list().length,0);assert.equal(downloads,0);store.close();
});

test('inputs survive restart, use isolated safe filenames, and reach Codex only after download',async()=>{
  const chat=await fs.mkdtemp(path.join(os.tmpdir(),'lark-inputs-')),root=path.join(chat,'input');const store=new Store(':memory:');
  const task=store.create(input,[],[image,{kind:'file',key:'file-key',name:'../../需求:报告.docx'}]).task;
  const calls=[];const downloader={downloadResource:async(message,attachment,file)=>{
    calls.push({message,key:attachment.key});await fs.writeFile(file,'input-data',{flag:'wx'});
    return {size:10,contentType:attachment.kind==='image'?'image/jpeg':'application/octet-stream'};
  }};
  assert.throws(()=>codexInput(task.instruction,store.inputs(task.id)),/尚未接收/);
  await prepareInputs(store,task,downloader,root);await prepareInputs(store,task,downloader,root);
  assert.equal(calls.length,2);assert.equal(calls[0].message,'message');
  const inputs=store.inputs(task.id);assert.equal(path.extname(inputs[0].path),'.jpg');
  assert.equal(path.basename(inputs[1].path),'需求_报告.docx','the name it was sent with, only cleaned');
  const flat=path.join(await fs.realpath(chat),'input');
  assert(inputs.every(file=>path.dirname(file.path)===flat),'one flat input folder, no per-task naming');
  const [text,...images]=codexInput(task.instruction,inputs),slash=p=>p.replaceAll('\\','/');
  assert(text.text.includes(`\n- ${slash(inputs[0].path)}\n- ${slash(inputs[1].path)}\n`),'every file by path');
  assert(text.text.endsWith('\n\n改成卡通风格'),'then the request, as typed');
  assert.deepEqual(images,[{type:'local_image',path:inputs[0].path}],'the image is also attached; the document is only a path');
  assert.equal(inputFilename(['..','..','secret.txt'].join(String.fromCharCode(92))),'secret.txt');
  // The same name sent again does not overwrite the earlier file.
  const again=store.create({...input,source_message_id:'again'},[],[{kind:'file',key:'file-key-2',name:'需求:报告.docx'}]).task;
  await prepareInputs(store,again,downloader,root);assert.equal(path.basename(store.inputs(again.id)[0].path),'需求_报告 (2).docx');
  await removeInputs([...inputs,...store.inputs(again.id)].map(i=>i.path),chat);assert.deepEqual(await fs.readdir(root),[],'cleanup deletes just the received files');
  // Only files directly in input/ are a task's: no other folder name and no sub-folder is treated as one.
  for(const stray of [path.join(chat,'inputs','a.txt'),path.join(root,'sub','b.txt')]){
    await fs.mkdir(path.dirname(stray),{recursive:true});await fs.writeFile(stray,'keep');
    await assert.rejects(removeInputs([stray],chat),/不属于/);assert.equal(await fs.readFile(stray,'utf8'),'keep');
  }
  const outside=path.join(path.dirname(chat),`outside-${process.pid}.txt`);await fs.writeFile(outside,'keep');
  await assert.rejects(removeInputs([outside],chat),/不属于/);assert.equal(await fs.readFile(outside,'utf8'),'keep','a file outside input/ is never deleted');await fs.rm(outside);
  store.close();await fs.rm(chat,{recursive:true});
});

test('download API uses the source message resource endpoint and enforces the streaming limit',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-stream-')),client=fakeClient();let payload;
  client.sdk.im.messageResource.get=async args=>{payload=args;return {headers:{'content-type':'image/png'},getReadableStream:()=>Readable.from([Buffer.from('PNG')])};};
  const result=await client.downloadResource('original-message',image,path.join(root,'image.png'));
  assert.deepEqual(payload,{path:{message_id:'original-message',file_key:image.key},params:{type:'image'}});assert.equal(result.size,3);
  assert.equal(await fs.readFile(path.join(root,'image.png'),'utf8'),'PNG');
  client.sdk.im.messageResource.get=async()=>({headers:{},getReadableStream:()=>Readable.from((function*(){for(let n=0;n<=100;n++)yield Buffer.alloc(1024*1024);})())});
  await assert.rejects(client.downloadResource('original-message',image,path.join(root,'large.png')),/100 MB/);
  const stream=Readable.from(['test']);client.sdk.im.messageResource.get=async()=>({headers:{'content-length':MAX_IMAGE_BYTES+1},getReadableStream:()=>stream});
  await assert.rejects(client.downloadResource('original-message',image,path.join(root,'declared-large.png')),/100 MB/);assert(stream.destroyed);
  client.close();await fs.rm(root,{recursive:true});
});

test('a rich-text event is deduplicated, downloaded, and handed to Codex with its original instruction',async()=>{
  const store=new Store(':memory:'),c=codex();let downloads=0;
  const service=new TaskService(store,c,{downloadResource:async(_message,_attachment,file)=>{
    downloads++;await fs.writeFile(file,'image-data');return {size:10,contentType:'image/png'};
  }});service.authorized=()=>true;
  const message=incomingMessage(event('post',{content:[[{tag:'img',image_key:image.key}],[{tag:'text',text:'改成卡通风格'}]]}),'bot');
  await service.receive(message);await service.receive(message);assert.equal(store.list().length,1);
  const task=store.list()[0];store.transition(task.id,'STARTING');await service.start(store.get(task.id));
  assert.equal(downloads,1);assert.equal(c.jobs.length,1);assert.equal(store.get(task.id).thread_id,'thread');
  const saved=store.inputs(task.id)[0].path,job=c.jobs[0];assert.equal(await fs.readFile(saved,'utf8'),'image-data','the file was there before Codex started');
  assert.equal(path.basename(path.dirname(saved)),'input','attachments land in the workspace\'s input/');assert.equal(path.basename(saved),'image-1.png');
  assert.equal(path.dirname(path.dirname(saved)),await fs.realpath(job.cwd),'and that workspace is where Codex works');
  assert(job.input[0].text.includes('改成卡通风格'));assert.deepEqual(job.input.slice(1),[{type:'local_image',path:saved}]);
  await removeInputs([saved],job.cwd);store.close();
});

test('failed and cancelled downloads do not start Codex, and partial files are removed',async()=>{
  const store=new Store(':memory:'),c=codex();
  const task=store.create(input,[],[image]).task;
  const service=new TaskService(store,c,{downloadResource:async(_message,_attachment,file)=>{await fs.writeFile(file,'partial');throw Error('缺少 im:resource 权限');}});
  store.transition(task.id,'STARTING');await service.start(store.get(task.id));
  assert.equal(store.get(task.id).status,'FAILED');assert.equal(store.get(task.id).error,'机器人罢工了，请联系 @钟丰骏-实习生');assert.equal(c.jobs.length,0);
  const folder=sessionInputs(store.session(store.get(task.id).session_id).workspace_path);assert.deepEqual(await fs.readdir(folder),[],'the partial download is gone');
  const cancelled=store.create({...input,source_message_id:'cancelled'},[],[image]).task;
  service.downloader={downloadResource:async(_message,_attachment,file)=>{await fs.writeFile(file,'data');store.update(cancelled.id,{cancel_requested:1});return {size:4,contentType:'image/png'};}};
  store.transition(cancelled.id,'STARTING');await service.start(store.get(cancelled.id));
  assert.equal(store.get(cancelled.id).status,'CANCELLED');assert.equal(c.jobs.length,0);store.close();
});

test('voice messages arrive as a file, and like any file they go to Codex as a path',()=>{
  const voice=incomingMessage({sender:{sender_type:'user',sender_id:{open_id:'u'}},tenant_key:'t',message:{message_id:'m',chat_id:'c',chat_type:'p2p',message_type:'audio',content:JSON.stringify({file_key:'k',duration:3000})}},'bot');
  assert.deepEqual(voice.attachments,[{kind:'file',key:'k',name:'语音消息.opus'}]);assert(!voice.error);
  assert.deepEqual(codexInput('',[{kind:'file',path:'C:/in/语音消息.opus'}]).map(x=>x.type),['text'],'there is no audio input: only the path');
});

test('in a group, other people mentioned in plain text keep their names instead of @_user_N placeholders',()=>{
  const mentions=[{key:'@_user_1',id:{open_id:'bot'},name:'机器人'},{key:'@_user_2',id:{open_id:'ou_x'},name:'小王'}];
  const message=incomingMessage(event('text',{text:'@_user_1 帮我问问 @_user_2 的进度'},{chat_type:'group',mentions}),'bot');
  assert.equal(message.text,' 帮我问问 @小王 的进度');
});

/** A message resource the way Feishu serves it: Range pieces of at most 32 MB with a Content-Range, an error past the end. */
function rangedResource(data,calls){
  return async(args,options)=>{
    const range=options?.headers?.Range;calls.push(range??'whole');
    if(!range)return {headers:{'content-length':data.length,'content-type':'video/mp4'},getReadableStream:()=>Readable.from([data])};
    const [,from,to]=/bytes=(\d+)-(\d+)/.exec(range).map(Number);
    if(to-from+1>32*1024*1024)throw Object.assign(Error('Range Not Satisfiable'),{response:{data:{code:234046,msg:'Range Not Satisfiable'}}});
    if(from>=data.length)throw Object.assign(Error('internal server error'),{response:{data:{code:40009,msg:'internal server error'}}});
    const end=Math.min(to,data.length-1);
    return {headers:{'content-range':`bytes ${from}-${end}/${data.length}`,'content-type':'video/mp4'},getReadableStream:()=>Readable.from([data.subarray(from,end+1)])};
  };
}

test('files and videos over 100 MB come in Range pieces into one file; the limit is checked before anything is fetched',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-ranged-')),client=fakeClient();
  const video={kind:'file',key:'file_v3_video',name:'P01.mp4'};
  const data=Buffer.alloc(65*1024*1024);for(let i=0;i<data.length;i+=4096)data[i]=i%251;
  let calls=[];client.sdk.im.messageResource.get=rangedResource(data,calls);
  const result=await client.downloadResource('message',video,path.join(root,'P01.mp4'));
  assert.equal(result.size,data.length);assert.equal(result.contentType,'video/mp4');
  assert.deepEqual(calls,['bytes=0-31457279','bytes=31457280-62914559','bytes=62914560-94371839']);
  assert((await fs.readFile(path.join(root,'P01.mp4'))).equals(data),'the pieces join into the file');
  // An empty file has no first piece to ask for: it comes without a range.
  calls=[];client.sdk.im.messageResource.get=rangedResource(Buffer.alloc(0),calls);
  assert.equal((await client.downloadResource('message',video,path.join(root,'empty.mp4'))).size,0);assert.deepEqual(calls,['bytes=0-31457279','whole']);
  // Too large: refused on the first answer's total, with a reason for the user.
  const huge=new Readable({read(){}});
  client.sdk.im.messageResource.get=async()=>({headers:{'content-range':`bytes 0-31457279/${MAX_INPUT_BYTES+1}`},getReadableStream:()=>huge});
  await assert.rejects(client.downloadResource('message',video,path.join(root,'huge.mp4')),error=>error.constructor.name==='InputRejected'&&/2 GB/.test(error.message));
  assert(huge.destroyed);
  client.close();await fs.rm(root,{recursive:true});
});

test('an attachment that is too large fails the task with its reason, not the generic one',async()=>{
  const store=new Store(':memory:'),c=codex();
  const task=store.create(input,[],[{kind:'file',key:'big',name:'素材.zip'}]).task;
  const client=fakeClient();client.sdk.im.messageResource.get=async()=>({headers:{'content-range':`bytes 0-1/${MAX_INPUT_BYTES+1}`},getReadableStream:()=>Readable.from([Buffer.alloc(2)])});
  const service=new TaskService(store,c,client);
  store.transition(task.id,'STARTING');await service.start(store.get(task.id));
  assert.equal(store.get(task.id).status,'FAILED');assert.match(store.get(task.id).error,/超过 2 GB/);assert.equal(c.jobs.length,0);
  client.close();store.close();
});
