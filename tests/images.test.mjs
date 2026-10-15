import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {EventEmitter} from 'node:events';
import Database from 'better-sqlite3';
import {Store} from '../dist/storage/database.js';
import {StreamState} from '../dist/desktop/stream.js';
import {TaskService} from '../dist/tasks/service.js';
import {Delivery} from '../dist/feishu/delivery.js';
import {FeishuClient} from '../dist/feishu/client.js';
import {stageGeneratedImage} from '../dist/feishu/images.js';
import {config} from '../dist/config.js';

const png=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=','base64');
const input={tenant_key:'tenant',user_id:'user',chat_id:'chat',source_message_id:'message',instruction:'给我生成一张番薯的图片'};
const image={type:'imageGeneration',id:'exec-test-image',status:'completed',result:png.toString('base64'),savedPath:null,failure:null};
function publish(stream,items,status='completed',revision=1){
  stream.receive({type:'broadcast',method:'thread-stream-state-changed',params:{hostId:'local',conversationId:'thread',change:{type:'snapshot',revision,
    conversationState:{turns:[{turnId:'old',status:'completed',items:[{...image,id:'old-image'}]},
      {turnId:'turn',status,items}]}}}});
}
function fakeFeishu(){
  const sent=[],uploads=[];
  return {sent,uploads,connected:true,raw:async(_method,url)=>url.endsWith('/cards')?{card_id:'card'}:{},
    uploadImage:async file=>{uploads.push({kind:'image',bytes:await fs.readFile(file)});return {image_key:'img-uploaded'};},
    upload:async file=>{uploads.push({kind:'file',bytes:await fs.readFile(file)});return {file_key:'file-uploaded'};},
    reply:async(_source,_chat,type,content,uuid)=>{sent.push({type,content,uuid});return {message_id:`message-${sent.length}`};}};
}

test('generated image without a final Markdown link survives restart and is sent as a native Feishu image once',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-image-out-')),db=path.join(root,'agent.sqlite');
  let store=new Store(db);const task=store.create(input).task;
  store.transition(task.id,'RUNNING',{thread_id:'thread',turn_id:'turn',workspace_path:path.join(root,'workspace')});
  const desktop=Object.assign(new EventEmitter(),{stream:new StreamState(),unfollow:()=>{}}),service=new TaskService(store,desktop);
  const items=[{type:'agentMessage',phase:'final_answer',text:'已生成番薯图片，图片已显示在上方。'},image,
    {...image,type:'mcpToolCall',id:'private-tool-image',savedPath:'/private/secret.png'}];
  publish(desktop.stream,items);service.onState('thread');
  assert.equal(store.get(task.id).status,'COMPLETED');assert.equal(store.images(task.id).length,1);
  await new Promise(resolve=>setImmediate(resolve));store.close();store=new Store(db);
  const client=fakeFeishu(),delivery=new Delivery(store,client);
  await delivery.tick();assert.equal(store.db.prepare('SELECT kind FROM task_artifacts').get().kind,'image');
  await delivery.tick();assert.deepEqual(client.uploads,[{kind:'image',bytes:png}]);
  const images=client.sent.filter(x=>x.type==='image');assert.equal(images.length,1);assert.deepEqual(images[0].content,{image_key:'img-uploaded'});
  // Redelivery and duplicated Desktop snapshots must not send a second image.
  store.enqueue(`view:${task.id}`,task.id,'VIEW',{});await delivery.tick();await delivery.tick();
  assert.equal(client.sent.filter(x=>x.type==='image').length,1);assert.equal(client.uploads.length,1);
  await delivery.stop();store.close();await fs.rm(root,{recursive:true});
});

test('image send retry retains the uploaded image key and the deduplication UUID',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-image-retry-')),store=new Store(':memory:'),task=store.create(input).task;
  store.recordImages(task.id,[{id:image.id,savedPath:null,base64:png.toString('base64')}]);
  store.transition(task.id,'COMPLETED',{thread_id:'thread',workspace_path:root,result:'done'});
  const client=fakeFeishu(),reply=client.reply;let fail=true;const uuids=[];
  client.reply=async(...args)=>{if(args[2]==='image'){uuids.push(args[4]);if(fail){fail=false;throw Error('lost reply response');}}return reply(...args);};
  const delivery=new Delivery(store,client);await delivery.tick();await delivery.tick();
  store.db.prepare('UPDATE delivery_outbox SET next_attempt_at=0').run();await delivery.tick();
  assert.equal(client.uploads.length,1);assert.equal(uuids.length,2);assert.equal(uuids[0],uuids[1]);
  assert.equal(client.sent.filter(x=>x.type==='image').length,1);await delivery.stop();store.close();await fs.rm(root,{recursive:true});
});

test('Desktop saved image path must match the exact thread and image item; symlinks cannot escape the cache',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-image-cache-')),cache=path.join(root,'generated_images','thread');await fs.mkdir(cache,{recursive:true});
  const source=path.join(cache,`${image.id}.png`);await fs.writeFile(source,png);
  const task={thread_id:'thread',workspace_path:path.join(root,'workspace')},item={id:image.id,savedPath:source,base64:null};
  const destination=await stageGeneratedImage(task,item,root);assert.deepEqual(await fs.readFile(destination),png);
  await assert.rejects(stageGeneratedImage(task,{...item,savedPath:path.join(root,'private.png')},root),/不属于/);
  await assert.rejects(stageGeneratedImage({...task,thread_id:'another-thread'},item,root),/不属于/);
  await assert.rejects(stageGeneratedImage(task,{...item,id:'../private'},root),/标识/);
  const outside=path.join(root,'private.png');await fs.writeFile(outside,png);await fs.rm(source);
  try{await fs.symlink(outside,source);await assert.rejects(stageGeneratedImage(task,item,root),/越界/);}catch(e){if(e.code!=='EPERM')throw e;}
  await fs.rm(root,{recursive:true});
});

test('failed and incomplete image items do not get delivered; inline image data is validated',async()=>{
  const stream=new StreamState();publish(stream,[{...image,status:'inProgress'},{...image,id:'failed',failure:'failed'}]);assert.deepEqual(stream.view('thread','turn').images,[]);
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-image-inline-')),task={thread_id:'thread',workspace_path:root};
  await assert.rejects(stageGeneratedImage(task,{id:'bad',savedPath:null,base64:Buffer.from('secret').toString('base64')}),/PNG/);
  await assert.rejects(stageGeneratedImage(task,{id:'bad',savedPath:null,base64:'https://example.com/image.png'}),/数据无效/);
  await fs.rm(root,{recursive:true});
});

test('saved images over 10 MB use file delivery and remain within the 30 MB attachment limit',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-image-large-')),oldHome=config.codexHome;config.codexHome=root;
  const store=new Store(':memory:'),client=fakeFeishu(),delivery=new Delivery(store,client);
  try{
    const source=path.join(root,'generated_images','thread',`${image.id}.png`);await fs.mkdir(path.dirname(source),{recursive:true});
    const bytes=Buffer.alloc(10*1024*1024+1);png.copy(bytes);await fs.writeFile(source,bytes);
    const task=store.create(input).task;store.recordImages(task.id,[{id:image.id,savedPath:source,base64:null}]);
    store.transition(task.id,'COMPLETED',{thread_id:'thread',workspace_path:path.join(root,'workspace'),result:'done'});
    await delivery.tick();
    // Delivery must keep working from the staged file after Desktop clears its cache.
    await fs.rm(source);store.enqueue(`view:${task.id}`,task.id,'VIEW',{});
    await delivery.tick();await delivery.tick();assert.equal(client.uploads[0].kind,'file');assert.equal(client.sent.filter(x=>x.type==='file').length,1);assert.equal(store.due().length,0);
  }finally{config.codexHome=oldHome;await delivery.stop();store.close();await fs.rm(root,{recursive:true});}
});

test('v3 migration preserves file artifacts while adding image metadata and image upload uses the SDK image endpoint',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-image-migration-')),file=path.join(root,'agent.sqlite');let store=new Store(file);
  const task=store.create(input).task;store.db.prepare('INSERT INTO task_artifacts(id,task_id,path,name,size,file_key) VALUES(?,?,?,?,?,?)').run('artifact',task.id,'file.txt','file.txt',4,'file-key');store.close();
  const db=new Database(file);db.exec('DROP TABLE task_generated_images; ALTER TABLE task_artifacts DROP COLUMN kind; PRAGMA user_version=3');db.close();
  store=new Store(file);assert.equal(store.db.prepare('SELECT kind,file_key FROM task_artifacts').get().kind,'file');assert.equal(store.db.prepare('SELECT file_key FROM task_artifacts').get().file_key,'file-key');assert.deepEqual(store.images(task.id),[]);store.close();
  const client=new FeishuClient({appId:'cli_offline_test',appSecret:'offline-test-placeholder'}),source=path.join(root,'image.png');await fs.writeFile(source,png);let received;
  client.sdk.im.image.create=async({data})=>{received={type:data.image_type,bytes:Buffer.concat(await Array.fromAsync(data.image))};return {code:0,data:{image_key:'img-test'}};};
  assert.deepEqual(await client.uploadImage(source),{image_key:'img-test'});assert.deepEqual(received,{type:'message',bytes:png});client.close();await fs.rm(root,{recursive:true});
});
