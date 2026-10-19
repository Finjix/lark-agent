import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {Store} from '../dist/storage/database.js';
import {Delivery} from '../dist/feishu/delivery.js';
import {config} from '../dist/config.js';
import {purge,housekeeping} from '../dist/maintenance.js';

const DAY=86400000;
/** The moment `days` from now. purge judges files by the `now` it is given, and a file made just now cannot be backdated, so time moves instead. */
const after=days=>Date.now()+days*DAY;
const write=async(file,text='x')=>{await fs.mkdir(path.dirname(file),{recursive:true});await fs.writeFile(file,text);return file;};
const exists=file=>fs.access(file).then(()=>true,()=>false);
const input=(user,message=`m-${user}`)=>({tenant_key:'t',user_id:user,chat_id:'c',source_message_id:message,instruction:'x'});

/** A project in a temporary folder: data, user/<name>/workspace for each name given, and a Codex home. */
async function project(...names){
  const base=await fs.mkdtemp(path.join(os.tmpdir(),'lark-purge-')),store=new Store(':memory:');
  const roots={data:path.join(base,'data'),user:path.join(base,'user'),codexHome:path.join(base,'codex')};
  const workspace=name=>path.join(roots.user,name,'workspace');
  names.forEach((name,i)=>store.userDirName('t',`u${i+1}`,name));
  return {base,store,roots,workspace,done:async()=>{store.close();await fs.rm(base,{recursive:true,force:true});}};
}

test('what nobody touched for seven days goes from the logs, the workspaces and Codex\'s home; everything else stays',async()=>{
  const p=await project('李小明','小红');
  const gone=[
    await write(path.join(p.roots.data,'logs','2026-10-01-1.jsonl')),
    await write(path.join(p.workspace('李小明'),'input','需求.pdf')),await write(path.join(p.workspace('李小明'),'a','b','c','deep.txt')),
    await write(path.join(p.workspace('李小明'),'.hidden','h')),await write(path.join(p.workspace('李小明'),'report.md')),await write(path.join(p.workspace('小红'),'x.png')),
    await write(path.join(p.roots.codexHome,'sessions','2026','10','01','rollout-1.jsonl')),await write(path.join(p.roots.codexHome,'generated_images','thread','img.png')),
    await write(path.join(p.roots.codexHome,'log','codex-login.log')),await write(path.join(p.roots.codexHome,'.sandbox','sandbox.2026-10-01.log')),
    await write(path.join(p.roots.codexHome,'.tmp','plugins-clone','.git','objects','pack')),
  ];
  const kept=[
    await write(path.join(p.roots.data,'backup-1.sqlite')),await write(path.join(p.roots.data,'control.token')),await write(path.join(p.roots.data,'logs','notes.txt')),
    ...['auth.json','config.toml','installation_id','state_5.sqlite','thread_history_1.sqlite'].map(name=>path.join(p.roots.codexHome,name)),
    path.join(p.roots.codexHome,'.sandbox','setup.json'),path.join(p.roots.codexHome,'skills','.system','imagegen','SKILL.md'),path.join(p.roots.codexHome,'tmp','arg0','.lock'),
    path.join(p.roots.user,'李小明','plugin-one','tokens.json'),
  ];
  for(const file of kept)await write(file);
  try{
    const young=await purge(p.store,{roots:p.roots,now:after(6)});
    assert.deepEqual([young.logs.files,young.workspaces.files,young.codex.files],[0,0,0],'not yet seven days');
    for(const file of [...gone,...kept])assert(await exists(file),file);

    const old=await purge(p.store,{roots:p.roots,now:after(8)});
    assert.deepEqual([old.logs.files,old.workspaces.files,old.codex.files],[1,5,5]);
    assert(old.workspaces.bytes>0);
    for(const file of gone)assert(!await exists(file),`${file} should be gone`);
    for(const file of kept)assert(await exists(file),`${file} should stay`);
    // The folders that held them are gone too, but the roots of what was swept are not.
    assert(!await exists(path.join(p.workspace('李小明'),'a')));assert(!await exists(path.join(p.roots.codexHome,'sessions','2026')));
    for(const root of [p.workspace('李小明'),p.workspace('小红'),path.join(p.roots.data,'logs'),path.join(p.roots.codexHome,'sessions'),path.join(p.roots.codexHome,'.sandbox')])assert(await exists(root),root);
    assert.deepEqual(await purge(p.store,{roots:p.roots,now:after(8)}),{logs:{files:0,bytes:0},workspaces:{files:0,bytes:0},codex:{files:0,bytes:0}},'nothing left to do');
  }finally{await p.done();}
});

test('a file in use stays however long ago it was made, and so does the folder around it',async()=>{
  const p=await project('用户');
  const worked=await write(path.join(p.workspace('用户'),'project','notes.md')),idle=await write(path.join(p.workspace('用户'),'project','old.md'));
  // Edited again six days from now (the clock of this test): at day 8 it was last touched two days ago.
  await fs.utimes(worked,new Date(after(6)),new Date(after(6)));
  try{
    await purge(p.store,{roots:p.roots,now:after(8)});
    assert(await exists(worked));assert(!await exists(idle));assert(await exists(path.dirname(worked)));
    await purge(p.store,{roots:p.roots,now:after(14)});
    assert(!await exists(worked)&&!await exists(path.dirname(worked)));
  }finally{await p.done();}
});

test('links are removed as links and never followed, and a linked workspace is not swept at all',async t=>{
  const p=await project('链接','外链');
  const outside=await fs.mkdtemp(path.join(os.tmpdir(),'lark-purge-outside-')),secret=await write(path.join(outside,'secret.txt'),'keep me');
  await fs.mkdir(p.workspace('链接'),{recursive:true});await fs.mkdir(path.join(p.roots.user,'外链'),{recursive:true});
  try{
    try{await fs.symlink(outside,path.join(p.workspace('链接'),'link'),'junction');await fs.symlink(outside,p.workspace('外链'),'junction');}
    catch(e){if(e.code==='EPERM')return t.skip('no permission to create links');throw e;}
    await write(path.join(p.workspace('链接'),'plain.txt'));
    const purged=await purge(p.store,{roots:p.roots,now:after(8)});
    assert(!await exists(path.join(p.workspace('链接'),'link')),'the link itself goes');assert(!await exists(path.join(p.workspace('链接'),'plain.txt')));
    assert.equal(await fs.readFile(secret,'utf8'),'keep me','what it pointed at is untouched');
    assert.equal(purged.workspaces.files,2);
    assert(await exists(secret));assert((await fs.lstat(p.workspace('外链'))).isSymbolicLink(),'a workspace that is a link is left alone');
  }finally{await fs.rm(p.workspace('外链'),{force:true}).catch(()=>{});await fs.rm(outside,{recursive:true,force:true});await p.done();}
});

test('a user with an unfinished task keeps their workspace; the files of Codex wait until no task is running',async()=>{
  const p=await project('忙','闲');
  const busyFile=await write(path.join(p.workspace('忙'),'a.txt')),idleFile=await write(path.join(p.workspace('闲'),'b.txt')),log=await write(path.join(p.roots.data,'logs','x.jsonl'));
  try{
    const task=p.store.create(input('u1')).task;
    await purge(p.store,{roots:p.roots,now:after(8)});
    assert(await exists(busyFile),'a queued task counts');assert(!await exists(idleFile),'another user is swept regardless');assert(!await exists(log));
    // Codex's files: untouched while a task runs, swept once none does.
    const session=await write(path.join(p.roots.codexHome,'sessions','rollout.jsonl'));
    p.store.transition(task.id,'RUNNING');
    await purge(p.store,{roots:p.roots,now:after(8)});
    assert(await exists(session),'a task is running');assert(await exists(busyFile));
    p.store.transition(task.id,'COMPLETED');
    await purge(p.store,{roots:p.roots,now:after(8)});
    assert(!await exists(session));assert(!await exists(busyFile),'its owner is no longer busy');
  }finally{await p.done();}
});

test('an aborted purge stops, and a directory name that is not a plain name is never used',async()=>{
  const p=await project('正常');
  const file=await write(path.join(p.workspace('正常'),'a.txt')),stray=await write(path.join(p.base,'workspace','precious.txt'));
  p.store.db.prepare('INSERT INTO user_dirs(tenant_key,user_id,name,created_at) VALUES(?,?,?,?)').run('t','u9','..',1);
  p.store.db.prepare('INSERT INTO user_dirs(tenant_key,user_id,name,created_at) VALUES(?,?,?,?)').run('t','u8','a/b',1);
  try{
    const stop=new AbortController();stop.abort();
    await purge(p.store,{roots:p.roots,now:after(8),signal:stop.signal});assert(await exists(file));
    await purge(p.store,{roots:p.roots,now:after(8)});
    assert(!await exists(file));assert(await exists(stray),'user/../workspace is not a user\'s workspace');
  }finally{await p.done();}
});

test('housekeeping never rejects and does not start a second round while one is running',async()=>{
  // It works on the configured folders: Codex's home must not be the project's own (the other folders are temporary under `npm test`).
  const home=config.codexHome;config.codexHome=await fs.mkdtemp(path.join(os.tmpdir(),'lark-housekeeping-'));
  try{
    const store=new Store(':memory:'),first=housekeeping(store),second=housekeeping(store);
    assert.equal(first,second);await first;store.close();
    await housekeeping(store);
  }finally{await fs.rm(config.codexHome,{recursive:true,force:true});config.codexHome=home;}
});

test('an upload whose file is gone fails for good instead of being retried',async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'lark-gone-')),s=new Store(':memory:'),task=s.create(input('u1')).task;
  s.transition(task.id,'COMPLETED',{workspace_path:dir,result:'好了'});
  s.db.prepare('INSERT INTO task_artifacts(id,task_id,path,name,size,kind) VALUES(?,?,?,?,?,?)').run('a1',task.id,path.join(dir,'cleaned.txt'),'cleaned.txt',1,'file');
  s.enqueue('file:a1',task.id,'FILE',{artifactId:'a1'});
  const fake={connected:true,raw:async(method,url)=>url.endsWith('/cards')?{card_id:'card'}:{},reply:async()=>({message_id:'m'}),upload:async()=>({file_key:'k'}),uploadImage:async()=>({image_key:'i'})};
  const delivery=new Delivery(s,fake);await delivery.tick();
  assert.equal(s.db.prepare("SELECT status FROM delivery_outbox WHERE id='file:a1'").get().status,'FAILED');
  await delivery.stop();s.close();await fs.rm(dir,{recursive:true,force:true});
});
