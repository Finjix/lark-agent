import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {Store} from '../dist/storage/database.js';
import {ownedArtifact,artifactLinks,outputFiles,Delivery} from '../dist/feishu/delivery.js';

function input(message='m1',user='u1',tenant='t1'){return {tenant_key:tenant,user_id:user,chat_id:'chat-'+user,source_message_id:message,instruction:'任务 '+message};}
function memory(){return new Store(':memory:');}
const notices=s=>s.due().filter(r=>r.kind==='NOTICE').map(r=>JSON.parse(r.payload).text);

test('file collection rejects traversal and symlinks, accepts spaces and unicode',async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'lark-files-')),workspace=path.join(dir,'任务 空格');await fs.mkdir(workspace);
  const file=path.join(workspace,'报告.txt'),outside=path.join(dir,'secret.txt');await fs.writeFile(file,'safe');await fs.writeFile(outside,'secret');
  assert.equal((await ownedArtifact(workspace,file)).name,'报告.txt');await assert.rejects(ownedArtifact(workspace,outside));
  const linked=path.join(workspace,'linked');try{await fs.symlink(dir,linked,'junction');await assert.rejects(ownedArtifact(workspace,path.join(linked,'secret.txt')));}catch(e){if(e.code!=='EPERM')throw e;}
  assert.deepEqual(artifactLinks('[报告](<D:/测试 空格/报告.txt>)'),['D:/测试 空格/报告.txt']);await fs.rm(dir,{recursive:true});
});

test('reply links: parentheses inside a path are kept, other markdown is not mistaken for a file',()=>{
  assert.deepEqual(artifactLinks('[a](<D:/x/in/report (2).pdf>) [b](D:/x/out/report (2).pdf) [c](D:\\x\\a.txt:12)'),['D:/x/in/report (2).pdf','D:/x/out/report (2).pdf','D:\\x\\a.txt']);
  assert.deepEqual(artifactLinks('[site](https://example.com/a) [rel](docs/a.md) [anchor](#top) [open](<D:/x/unclosed.txt) next line)'),[]);
  assert.deepEqual(artifactLinks('see [a](D:/x/a.txt) and (parenthetical) then [b](D:/x/b (1).txt)'),['D:/x/a.txt','D:/x/b (1).txt']);
});

test('whatever the agent leaves in the workspace is delivered even when the reply does not link it',async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'lark-scan-')),workspace=path.join(dir,'workspace');
  await fs.mkdir(path.join(workspace,'sub'),{recursive:true});
  await fs.writeFile(path.join(workspace,'report.txt'),'r');await fs.writeFile(path.join(workspace,'sub','data.csv'),'d');await fs.writeFile(path.join(workspace,'chart.png'),'p');
  await fs.writeFile(path.join(workspace,'.scratch'),'x');await fs.writeFile(path.join(dir,'elsewhere.txt'),'never');
  const s=memory(),task=s.create(input()).task;s.transition(task.id,'COMPLETED',{started_at:task.created_at,workspace_path:workspace,result:`done, see [report](<${path.join(workspace,'report.txt')}>)`});
  const f={connected:true,raw:async(method,url)=>url.endsWith('/cards')?{card_id:'card'}:{},reply:async()=>({message_id:'message'}),upload:async()=>({file_key:'file'}),uploadImage:async()=>({image_key:'image'})};
  const delivery=new Delivery(s,f);await delivery.tick();
  const rows=s.db.prepare('SELECT name,kind FROM task_artifacts WHERE task_id=? ORDER BY name').all(task.id).map(r=>`${r.name}:${r.kind}`);
  assert.deepEqual(rows,['chart.png:image','data.csv:file','report.txt:file'],'linked and unlinked files once each; dotfiles and files outside the folder are not sent');
  await delivery.stop();s.close();await fs.rm(dir,{recursive:true});
  const many=await fs.mkdtemp(path.join(os.tmpdir(),'lark-scan-many-'));for(let n=0;n<25;n++)await fs.writeFile(path.join(many,`f${String(n).padStart(2,'0')}.txt`),'x');
  assert.equal((await outputFiles(many)).length,10,'capped');await fs.rm(many,{recursive:true});
});

test('hidden folders are not sent unless the reply links them',async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'lark-shots-'));await fs.mkdir(path.join(dir,'.screenshots'));await fs.mkdir(path.join(dir,'input'));await fs.writeFile(path.join(dir,'input','sent-by-user.pdf'),'u');
  await fs.writeFile(path.join(dir,'.screenshots','look.png'),'p');await fs.writeFile(path.join(dir,'report.txt'),'r');
  assert.deepEqual((await outputFiles(dir)).map(f=>path.basename(f)),['report.txt'],'neither the hidden folder nor what the user attached');await fs.rm(dir,{recursive:true});
});

test('a task owns only the files that appeared in its folder while it ran',async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'lark-window-'));await fs.writeFile(path.join(dir,'report.txt'),'r');
  const now=Date.now(),names=async window=>(await outputFiles(dir,window)).map(f=>path.basename(f));
  assert.deepEqual(await names({from:now-5000,to:now+5000}),['report.txt'],'created during the task');
  assert.deepEqual(await names({from:now-60000,to:now-30000}),[],'a file of a later task is not this task\'s');
  assert.deepEqual(await names({from:now+30000,to:now+60000}),[],'a file from before the task started is not sent again');
  // A copy keeps the old modification time but is a new file, so asking the agent to resend something still works.
  const old=path.join(dir,'old.txt');await fs.writeFile(old,'o');const past=new Date(now-86400000);await fs.utimes(old,past,past);
  const copy=path.join(dir,'copy.txt');await fs.copyFile(old,copy);
  assert((await names({from:now-5000,to:now+5000})).includes('copy.txt'),'a copied file counts by when it was created here');
  await fs.rm(dir,{recursive:true});
});

test('a message that never ran delivers no files, even with the workspace of the task it was to go into',async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'lark-never-ran-'));await fs.writeFile(path.join(dir,'report.docx'),'made by the running task');
  const s=memory(),task=s.create(input()).task;s.update(task.id,{workspace_path:dir});s.transition(task.id,'CANCELLED');
  const f={connected:true,raw:async(method,url)=>url.endsWith('/cards')?{card_id:'card'}:{},reply:async()=>({message_id:'message'}),upload:async()=>({file_key:'file'}),uploadImage:async()=>({image_key:'image'})};
  const delivery=new Delivery(s,f);await delivery.tick();
  assert.equal(s.db.prepare('SELECT count(*) c FROM task_artifacts').get().c,0,'the running task delivers its own files');
  await delivery.stop();s.close();await fs.rm(dir,{recursive:true});
});

test('replies still collect and deliver linked files; repeated delivery does not resend the file',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-raw-delivery-')),s=new Store(':memory:'),task=s.create(input('file')).task;
  const report=path.join(root,'交付.txt');await fs.writeFile(report,'原文交付校验');
  const answer=`已完成，[下载文件](<${report}>)`;
  s.transition(task.id,'COMPLETED',{started_at:task.created_at,thread_id:'thread',workspace_path:root,result:answer});
  const sent=[],uploads=[],api={connected:true,raw:async(_method,url)=>url.endsWith('/cards')?{card_id:'card'}:{},reply:async(...args)=>{sent.push(args);return {message_id:'sent'};},upload:async file=>{uploads.push(await fs.readFile(file,'utf8'));return {file_key:'uploaded'};}};
  const delivery=new Delivery(s,api);await delivery.tick();await delivery.tick();await delivery.tick();
  assert.deepEqual(uploads,['原文交付校验']);assert.equal(sent.filter(a=>a[2]==='file').length,1);assert.equal(s.get(task.id).result,answer);
  await delivery.stop();await new Promise(r=>setImmediate(r));s.close();await fs.rm(root,{recursive:true});
});

test('a link to a file outside the delivery folder is ignored without a notice and never uploaded',async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'lark-rejected-')),workspace=path.join(dir,'workspace');await fs.mkdir(workspace);
  const outside=path.join(dir,'outside.txt');await fs.writeFile(outside,'never upload');const s=memory(),task=s.create(input()).task;
  s.transition(task.id,'COMPLETED',{workspace_path:workspace,result:`[file](<${outside}>)`});let uploaded=false;
  const f={connected:true,raw:async(method,url)=>url.endsWith('/cards')?{card_id:'card'}:{},reply:async()=>({message_id:'message'}),upload:async()=>{uploaded=true;}};
  const delivery=new Delivery(s,f);await delivery.tick();assert.equal(s.db.prepare("SELECT count(*) c FROM delivery_outbox WHERE kind='NOTICE'").get().c,0,'no notice');assert.equal(s.db.prepare('SELECT count(*) c FROM task_artifacts').get().c,0);assert.equal(uploaded,false);
  await delivery.stop();s.close();await fs.rm(dir,{recursive:true});
});

test('a delivered file with parentheses in its name is sent, a link to a missing file inside the folder is still reported, links elsewhere are not',async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'lark-links-')),workspace=path.join(dir,'workspace'),input_=path.join(dir,'input');await fs.mkdir(workspace);await fs.mkdir(input_);
  const named=path.join(workspace,'report (2).pdf'),mine=path.join(input_,'photo (2).png');await fs.writeFile(named,'r');await fs.writeFile(mine,'m');
  const s=memory(),task=s.create(input()).task;
  s.transition(task.id,'COMPLETED',{started_at:task.created_at,workspace_path:workspace,result:`[报告](<${named}>) [你的图](<${mine}>) [站内](/docs/intro) [丢失](<${path.join(workspace,'missing.txt')}>)`});
  const f={connected:true,raw:async(method,url)=>url.endsWith('/cards')?{card_id:'card'}:{},reply:async()=>({message_id:'message'}),upload:async()=>({file_key:'file'}),uploadImage:async()=>({image_key:'image'})};
  const delivery=new Delivery(s,f);await delivery.tick();
  assert.deepEqual(s.db.prepare('SELECT name FROM task_artifacts WHERE task_id=?').all(task.id).map(r=>r.name),['report (2).pdf']);
  const notices=s.db.prepare("SELECT payload FROM delivery_outbox WHERE kind='NOTICE'").all().map(r=>JSON.parse(r.payload).text);
  assert.equal(notices.length,1);assert(notices[0].includes('missing.txt')&&notices[0].includes('附件未发送'));
  await delivery.stop();s.close();await fs.rm(dir,{recursive:true});
});

test('a linked file that cannot be sent is reported while the final reply is still delivered',async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'review-file-')),s=new Store(':memory:'),task=s.create(input('img')).task;
  const folder=path.join(dir,'素材');await fs.mkdir(folder);
  s.transition(task.id,'COMPLETED',{started_at:task.created_at,workspace_path:dir,thread_id:'thr',result:`这是最终答案，见 [素材](<${folder}>)`});
  const calls=[],fake={connected:true,raw:async(method,url,data)=>{calls.push({method,url,data});return url.endsWith('/cards')?{card_id:'card'}:{};},reply:async()=>({message_id:'m'})};
  const delivery=new Delivery(s,fake);await delivery.tick();
  const final=calls.filter(c=>c.method==='PUT'&&c.data.card).map(c=>JSON.parse(c.data.card.data)).at(-1);
  assert.match(final.body.elements[0].content,/^这是最终答案/);
  assert.equal(s.db.prepare('SELECT status FROM delivery_outbox WHERE id=?').get(`view:${task.id}`).status,'DELIVERED');
  assert(notices(s).some(t=>t.includes('素材')&&t.includes('附件未发送')&&t.includes('不是文件')));
  await delivery.tick();await delivery.tick(); // reported once, not on every pass
  assert.equal(s.db.prepare("SELECT count(*) c FROM delivery_outbox WHERE kind='NOTICE'").get().c,1);
  assert.equal(s.db.prepare("SELECT status FROM delivery_outbox WHERE kind='NOTICE'").get().status,'DELIVERED');
  await delivery.stop();s.close();await fs.rm(dir,{recursive:true,force:true});
});

test('a file that vanishes while the workspace is being looked through does not hold back the final reply or the other files',async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'review-vanish-')),s=new Store(':memory:'),task=s.create(input('vanish')).task;
  s.transition(task.id,'STARTING',{started_at:Date.now()-5000,workspace_path:dir});
  await fs.writeFile(path.join(dir,'answer.txt'),'交付');await fs.writeFile(path.join(dir,'tmp-1.lock'),'deleted by a process still running');
  s.transition(task.id,'COMPLETED',{result:'完成，见 answer.txt'});
  const stat=fs.stat;fs.stat=async(file,...rest)=>{if(String(file).endsWith('tmp-1.lock'))await fs.rm(file);return stat(file,...rest);};
  try{
    const calls=[],fake={raw:async(method,url,data)=>{calls.push({method,url,data});return url.endsWith('/cards')?{card_id:'card'}:{};},reply:async(_m,_c,type)=>{calls.push({type});return {message_id:'m'};},upload:async()=>({file_key:'fk'})};
    const delivery=new Delivery(s,fake);await delivery.tick();await delivery.tick();
    assert.equal(s.db.prepare('SELECT status FROM delivery_outbox WHERE id=?').get(`view:${task.id}`).status,'DELIVERED');
    assert.deepEqual(s.db.prepare('SELECT name FROM task_artifacts').all().map(r=>r.name),['answer.txt']);
    assert(calls.some(c=>c.type==='file'),'the deliverable went out');
    await delivery.stop();
  }finally{fs.stat=stat;s.close();await fs.rm(dir,{recursive:true,force:true});}
});
