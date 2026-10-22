import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {EventEmitter} from 'node:events';
import {Store} from '../dist/storage/database.js';
import {TaskService} from '../dist/tasks/service.js';
import {Delivery} from '../dist/feishu/delivery.js';
import {FeishuClient} from '../dist/feishu/client.js';
import {stageGeneratedImage,generatedImages} from '../dist/feishu/images.js';
import {config} from '../dist/config.js';

const png=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=','base64');
const input={tenant_key:'tenant',user_id:'user',chat_id:'chat',source_message_id:'message',instruction:'给我生成一张番薯的图片'};
const IMAGE='exec-test-image';
function fakeFeishu(){
  const sent=[],uploads=[];
  return {sent,uploads,connected:true,raw:async(_method,url)=>url.endsWith('/cards')?{card_id:'card'}:{},
    uploadImage:async file=>{uploads.push({kind:'image',bytes:await fs.readFile(file)});return {image_key:'img-uploaded'};},
    upload:async file=>{uploads.push({kind:'file',bytes:await fs.readFile(file)});return {file_key:'file-uploaded'};},
    reply:async(_source,_chat,type,content,uuid)=>{sent.push({type,content,uuid});return {message_id:`message-${sent.length}`};}};
}
/** Runs with a Codex home of its own, as Codex keeps generated images under <home>/generated_images/<thread>. */
async function withHome(fn){
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-image-')),saved=config.codexHome;config.codexHome=root;
  try{await fn(root);}finally{config.codexHome=saved;await fs.rm(root,{recursive:true,force:true});}
}
const generated=(root,thread=`thread`,name=`${IMAGE}.png`)=>path.join(root,'generated_images',thread,name);
async function save(root,thread,name,bytes=png){const file=generated(root,thread,name);await fs.mkdir(path.dirname(file),{recursive:true});await fs.writeFile(file,bytes);return file;}

test('a generated image without a final Markdown link survives restart and is sent as a native Feishu image once',()=>withHome(async root=>{
  const db=path.join(root,'agent.sqlite');
  let store=new Store(db);
  // Codex makes the image while the task runs; its events do not mention it.
  const codex=Object.assign(new EventEmitter(),{ready:true,run:async function*(){yield {type:'thread.started',thread_id:'thread'};await save(root,'thread');await save(root,'other-thread','foreign.png');
    yield {type:'item.completed',item:{id:'a',type:'agent_message',text:'已生成番薯图片。'}};yield {type:'turn.completed',usage:{}};}});
  const service=new TaskService(store,codex);service.authorized=()=>true;
  const task=store.create(input).task;store.transition(task.id,'STARTING',{started_at:Date.now()});await service.start(store.get(task.id));
  assert.equal(store.get(task.id).status,'COMPLETED');assert.deepEqual(store.images(task.id).map(i=>i.id),[IMAGE]);
  await new Promise(resolve=>setImmediate(resolve));store.close();store=new Store(db);
  const client=fakeFeishu(),delivery=new Delivery(store,client);
  await delivery.tick();assert.equal(store.db.prepare('SELECT kind FROM task_artifacts').get().kind,'image');
  await delivery.tick();assert.deepEqual(client.uploads,[{kind:'image',bytes:png}]);
  const images=client.sent.filter(x=>x.type==='image');assert.equal(images.length,1);assert.deepEqual(images[0].content,{image_key:'img-uploaded'});
  // Redelivery of the card must not send a second image.
  store.enqueue(`view:${task.id}`,task.id,'VIEW',{});await delivery.tick();await delivery.tick();
  assert.equal(client.sent.filter(x=>x.type==='image').length,1);assert.equal(client.uploads.length,1);
  await delivery.stop();store.close();
}));

test('image send retry retains the uploaded image key and the deduplication UUID',()=>withHome(async root=>{
  const store=new Store(':memory:'),task=store.create(input).task;
  store.recordImages(task.id,[{id:IMAGE,savedPath:await save(root,'thread')}]);
  store.transition(task.id,'COMPLETED',{started_at:task.created_at,thread_id:'thread',workspace_path:path.join(root,'workspace'),result:'done'});
  const client=fakeFeishu(),reply=client.reply;let fail=true;const uuids=[];
  client.reply=async(...args)=>{if(args[2]==='image'){uuids.push(args[4]);if(fail){fail=false;throw Error('lost reply response');}}return reply(...args);};
  const delivery=new Delivery(store,client);await delivery.tick();await delivery.tick();
  store.db.prepare('UPDATE delivery_outbox SET next_attempt_at=0').run();await delivery.tick();
  assert.equal(client.uploads.length,1);assert.equal(uuids.length,2);assert.equal(uuids[0],uuids[1]);
  assert.equal(client.sent.filter(x=>x.type==='image').length,1);await delivery.stop();store.close();
}));

test('a saved image path must match the exact thread and image; symlinks cannot escape the image folder',()=>withHome(async root=>{
  const source=await save(root,'thread');
  const task={thread_id:'thread',workspace_path:path.join(root,'workspace')},item={id:IMAGE,savedPath:source};
  const destination=await stageGeneratedImage(task,item,root);assert.deepEqual(await fs.readFile(destination),png);
  await assert.rejects(stageGeneratedImage(task,{...item,savedPath:path.join(root,'private.png')},root),/不属于/);
  await assert.rejects(stageGeneratedImage({...task,thread_id:'another-thread'},item,root),/不属于/);
  await assert.rejects(stageGeneratedImage(task,{...item,id:'../private'},root),/标识/);
  await fs.writeFile(source,'not a png');await assert.rejects(stageGeneratedImage(task,item,root),/PNG/,'what is not an image never leaves the folder');
  const outside=path.join(root,'private.png');await fs.writeFile(outside,png);await fs.rm(source);
  try{await fs.symlink(outside,source);await assert.rejects(stageGeneratedImage(task,item,root),/越界/);}catch(e){if(e.code!=='EPERM')throw e;}
}));

test('the images of a task are the PNG files that appeared in its own chat\'s folder while it ran',()=>withHome(async root=>{
  const since=Date.now();
  await save(root,'thread','fresh.png');await save(root,'thread','notes.txt',Buffer.from('x'));await save(root,'thread','bad name.png');await save(root,'other','foreign.png');
  const old=await save(root,'thread','earlier-task.png');const past=new Date(since-3600000);await fs.utimes(old,past,past);
  assert.deepEqual((await generatedImages('thread',since)).map(i=>i.id),['fresh']);
  assert.deepEqual(await generatedImages('never-had-images',since),[],'no folder, no images');
  assert.deepEqual(await generatedImages('../thread',since),[],'a thread id cannot point elsewhere');
}));

test('saved images over 10 MB use file delivery and remain within the 30 MB attachment limit',()=>withHome(async root=>{
  const store=new Store(':memory:'),client=fakeFeishu(),delivery=new Delivery(store,client);
  try{
    const bytes=Buffer.alloc(10*1024*1024+1);png.copy(bytes);const source=await save(root,'thread',`${IMAGE}.png`,bytes);
    const task=store.create(input).task;store.recordImages(task.id,[{id:IMAGE,savedPath:source}]);
    store.transition(task.id,'COMPLETED',{started_at:task.created_at,thread_id:'thread',workspace_path:path.join(root,'workspace'),result:'done'});
    await delivery.tick();
    // Delivery must keep working from the staged file after the image folder is cleared.
    await fs.rm(source);store.enqueue(`view:${task.id}`,task.id,'VIEW',{});
    await delivery.tick();await delivery.tick();assert.equal(client.uploads[0].kind,'file');assert.equal(client.sent.filter(x=>x.type==='file').length,1);assert.equal(store.due().length,0);
  }finally{await delivery.stop();store.close();}
}));

test('image upload uses the SDK image endpoint, and a task without images has none',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-image-upload-')),store=new Store(':memory:');
  const task=store.create(input).task;assert.deepEqual(store.images(task.id),[]);store.close();
  const client=new FeishuClient({appId:'cli_offline_test',appSecret:'offline-test-placeholder'}),source=path.join(root,'image.png');await fs.writeFile(source,png);let received;
  client.sdk.im.image.create=async({data})=>{received={type:data.image_type,bytes:Buffer.concat(await Array.fromAsync(data.image))};return {code:0,data:{image_key:'img-test'}};};
  assert.deepEqual(await client.uploadImage(source),{image_key:'img-test'});assert.deepEqual(received,{type:'message',bytes:png});client.close();await fs.rm(root,{recursive:true});
});

test('a reply that links the generated image by its image-folder path does not raise a rejection beside the staged copy',()=>withHome(async root=>{
  const store=new Store(':memory:'),client=fakeFeishu(),delivery=new Delivery(store,client);
  try{
    const source=await save(root,'thread');
    const task=store.create(input).task;store.recordImages(task.id,[{id:IMAGE,savedPath:source}]);
    store.transition(task.id,'COMPLETED',{started_at:task.created_at,thread_id:'thread',workspace_path:path.join(root,'workspace'),result:`![番薯](${source})`});
    await delivery.tick();await delivery.tick();await delivery.tick();
    assert.equal(store.db.prepare("SELECT count(*) n FROM delivery_outbox WHERE kind='NOTICE'").get().n,0,'no "附件未发送" notice');
    assert.equal(client.sent.filter(x=>x.type==='image').length,1,'the image is still sent, once');
  }finally{await delivery.stop();store.close();}
}));
