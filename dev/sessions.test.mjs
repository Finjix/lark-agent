import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {Store} from '../dist/storage/database.js';
import {cleanup} from '../dist/maintenance.js';
import {config} from '../dist/config.js';

const input=(message='one',user='user',tenant='tenant')=>({tenant_key:tenant,user_id:user,chat_id:'chat',source_message_id:message,instruction:message});
/** A user's session bound to a Codex thread, working in `root`. */
function bound(s,task,thread,root){const session=s.sessionFor(task);s.bindSession(session.id,thread);if(root)s.setSessionWorkspace(session.id,root);return s.session(session.id);}
/** A finished task whose retention period is over, working in `workspace`. */
function expired(s,task,thread,workspace){s.transition(task.id,'COMPLETED',{thread_id:thread,workspace_path:workspace});s.update(task.id,{finished_at:1});}
/** Every delivery has gone out, so cleanup looks at the tasks. */
const delivered=s=>s.db.exec("UPDATE delivery_outbox SET status='DELIVERED'");
/** A moment after the retention period of files made now: cleanup judges a file by its own last change, and one made just now cannot be backdated. */
const later=()=>Date.now()+(config.RETENTION_DAYS+1)*86400000;
const artifact=(s,task,id,file)=>s.db.prepare('INSERT INTO task_artifacts(id,task_id,path,name,size,kind) VALUES(?,?,?,?,?,?)').run(id,task.id,file,path.basename(file),1,'file');
const exists=file=>fs.access(file).then(()=>true,()=>false);

test('session ownership survives restart and is isolated by tenant and user',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-session-db-')),file=path.join(root,'db.sqlite');let s=new Store(file);
  const a=s.create(input()).task,b=s.create(input('two')).task,c=s.create(input('other-one','other')).task,d=s.create(input('one','user','other-tenant')).task;
  const session=bound(s,a,'thread','workspace');
  assert.equal(s.sessionFor(b).id,session.id);assert.notEqual(s.sessionFor(c).id,session.id);assert.notEqual(s.sessionFor(d).id,session.id);
  assert.throws(()=>s.bindSession(session.id,'wrong-thread'),/其他/);
  s.close();s=new Store(file);assert.equal(s.sessionFor(s.get(b.id)).thread_id,'thread');assert.equal(s.get(b.id).session_id,session.id);
  s.close();await fs.rm(root,{recursive:true});
});

test('a chat replaced or reset keeps its history on the old session and is never shared between two users',()=>{
  const s=new Store(':memory:'),a=s.create(input()).task,other=s.create(input('x','other')).task;
  const first=bound(s,a,'thread-1','w');s.nameSession(first.id,'薯薯');
  const next=s.renewSession(s.get(a.id),'missing');
  assert.notEqual(next.id,first.id);assert.equal(next.thread_id,null);assert.equal(next.display_name,'薯薯','the name moves to the new session');
  assert.equal(s.session(first.id).thread_id,'thread-1');assert(s.session(first.id).retired_at);assert.equal(s.get(a.id).session_id,next.id);assert.equal(s.get(a.id).thread_id,null);
  assert.equal(s.sessions().length,1);
  assert.equal(s.resetSession('tenant','other'),false,'nothing to forget before the first chat');s.sessionFor(other);assert.equal(s.resetSession('tenant','other'),false,'a session without a chat either');
  bound(s,other,'thread-2');assert.equal(s.resetSession('tenant','other'),true);
  assert.equal(s.sessionFor(s.get(other.id)).thread_id,null);assert.equal(s.session(s.get(other.id).session_id).display_name,null);
  assert.deepEqual(s.db.pragma('foreign_key_check'),[]);s.close();
});

test('expiring a completed message removes only the files it delivered and keeps the workspace, the session and later files',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-session-retain-')),s=new Store(':memory:');
  const old=s.create(input()).task,session=bound(s,old,'thread',root);
  const oldFile=path.join(root,'old.txt'),laterFile=path.join(root,'later.txt');await fs.writeFile(oldFile,'old');await fs.writeFile(laterFile,'keep');
  expired(s,old,'thread',root);artifact(s,old,'a1',oldFile);delivered(s);
  await cleanup(s,later());
  assert(s.get(old.id).cleaned_at);assert.equal(s.session(session.id).thread_id,'thread');
  await assert.rejects(fs.stat(oldFile),{code:'ENOENT'});assert.equal(await fs.readFile(laterFile,'utf8'),'keep');assert((await fs.stat(root)).isDirectory());
  s.close();await fs.rm(root,{recursive:true});
});

test('a file an expired task delivered stays while it is still in use: a later task changed it, so it is judged by that change',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-session-inuse-')),s=new Store(':memory:');
  const old=s.create(input('old')).task;bound(s,old,'thread',root);
  const report=path.join(root,'report.xlsx'),attachment={kind:'file',key:'k',name:'brief.xlsx'};await fs.writeFile(report,'v1');
  expired(s,old,'thread',root);artifact(s,old,'a1',report);
  const sent=s.create(input('sent'),[],[attachment]).task;bound(s,sent,'thread',root);
  const brief=path.join(root,'input','brief.xlsx');await fs.mkdir(path.dirname(brief));await fs.writeFile(brief,'b');s.inputReady(sent.id,0,brief,1);expired(s,sent,'thread',root);
  delivered(s);
  // The tasks ended long ago, but a later task changed both files a day before this moment.
  await fs.writeFile(report,'v2');await fs.writeFile(brief,'edited');
  await cleanup(s,Date.now()+86400000);
  assert.equal(await fs.readFile(report,'utf8'),'v2','a delivered file still in use is kept');assert.equal(await fs.readFile(brief,'utf8'),'edited','so is an attachment');
  assert(s.get(old.id).cleaned_at&&s.get(sent.id).cleaned_at,'the tasks are done with: purge takes the files once nobody touches them');
  s.close();await fs.rm(root,{recursive:true});
});

test('cleanup removes inside the user\'s workspace only: a recorded path elsewhere stays, and a task whose folder is not its session\'s workspace is skipped',async()=>{
  const base=await fs.mkdtemp(path.join(os.tmpdir(),'lark-session-owner-')),root=path.join(base,'workspace'),elsewhere=path.join(base,'elsewhere'),s=new Store(':memory:');
  await fs.mkdir(root);await fs.mkdir(elsewhere);
  const write=async(file,text='x')=>{await fs.writeFile(file,text);return file;};
  // The record names a file beside the workspace as well as one inside it: only the one inside goes.
  const a=s.create(input('a')).task;bound(s,a,'thread-a',root);
  const mine=await write(path.join(root,'mine.txt')),foreign=await write(path.join(base,'foreign.txt'));
  expired(s,a,'thread-a',root);artifact(s,a,'a1',mine);artifact(s,a,'a2',foreign);
  // A task whose folder is some other folder than the workspace of its session: nothing shows that it owns it.
  const b=s.create(input('b','other')).task;bound(s,b,'thread-b',root);
  const theirs=await write(path.join(elsewhere,'theirs.txt'));expired(s,b,'thread-b',elsewhere);artifact(s,b,'b1',theirs);
  // A task with a folder but no session at all.
  const c=s.create(input('c','third')).task,orphan=await write(path.join(root,'orphan.txt'));expired(s,c,'thread-c',root);artifact(s,c,'c1',orphan);
  delivered(s);
  await cleanup(s,later());
  assert(s.get(a.id).cleaned_at);assert(!await exists(mine));assert.equal(await fs.readFile(foreign,'utf8'),'x','a path outside the workspace is never removed');
  assert.equal(s.get(b.id).cleaned_at,null,'skipped, not cleaned');assert(await exists(theirs));
  assert.equal(s.get(c.id).cleaned_at,null,'skipped, not cleaned');assert(await exists(orphan));
  s.close();await fs.rm(base,{recursive:true});
});

test('expiry also removes the attachments a task received, from input/ of the workspace and nowhere else',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-session-inputs-')),s=new Store(':memory:'),folder=path.join(root,'input');await fs.mkdir(folder);
  const attachment={kind:'file',key:'k',name:'a.txt'};
  const t=s.create(input('one'),[],[attachment]).task;bound(s,t,'thread',root);
  const received=path.join(folder,'a.txt'),neighbour=path.join(folder,'neighbour.txt');await fs.writeFile(received,'a');await fs.writeFile(neighbour,'n');
  s.inputReady(t.id,0,received,1);expired(s,t,'thread',root);
  // Another task's record that points at a file outside input/: the task is skipped and the file stays.
  const u=s.create(input('two','u2'),[],[attachment]).task;bound(s,u,'thread-u',root);
  const precious=path.join(root,'precious.txt');await fs.writeFile(precious,'p');s.inputReady(u.id,0,precious,1);expired(s,u,'thread-u',root);
  delivered(s);
  await cleanup(s,later());
  assert(s.get(t.id).cleaned_at);assert(!await exists(received));assert(await exists(neighbour),'only what this task received');assert((await fs.stat(folder)).isDirectory());
  assert.equal(s.get(u.id).cleaned_at,null);assert(await exists(precious));
  s.close();await fs.rm(root,{recursive:true});
});
