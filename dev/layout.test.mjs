import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import fs from 'node:fs';
import {config} from '../dist/config.js';
import {Store} from '../dist/storage/database.js';
import {userDirectory,userData,taskWorkspace,sessionInputs} from '../dist/layout.js';

const input=(user,tenant='t')=>({tenant_key:tenant,user_id:user,chat_id:'c',source_message_id:`m-${tenant}-${user}`,instruction:'x'});
/** A session for `user`, named like the Feishu display name once it is known. */
function session(store,user,name,tenant='t'){
  const task=store.create(input(user,tenant)).task,created=store.sessionFor(task);
  if(name)store.nameSession(created.id,name);
  return store.session(created.id);
}
const rel=file=>path.relative(config.user,file).split(path.sep).join('/');

test('each user gets user/<name>/data; a duplicate name gets a number and the name never changes',()=>{
  const store=new Store(':memory:');
  const first=session(store,'ou_1','李小明'),second=session(store,'ou_2','李小明'),third=session(store,'ou_3','李小明');
  assert.equal(rel(userDirectory(store,first)),'李小明');
  assert.equal(rel(userDirectory(store,second)),'李小明2');
  assert.equal(rel(userDirectory(store,third)),'李小明3');
  assert.equal(rel(userData(store,first)),'李小明/data');
  // Renaming in Feishu later does not move anything.
  store.nameSession(first.id,'完全不同的名字');assert.equal(rel(userDirectory(store,store.session(first.id))),'李小明');
  // Names compare case-insensitively because Windows folders do.
  assert.equal(rel(userDirectory(store,session(store,'ou_4','Li Ming'))),'Li Ming');assert.equal(rel(userDirectory(store,session(store,'ou_5','li ming'))),'li ming2');
  // The same open id in another tenant is another user.
  assert.equal(rel(userDirectory(store,session(store,'ou_1','李小明','other'))),'李小明4');
  store.close();
});

test('user directory names cannot escape user/ and fall back to the open id when no name is known',()=>{
  const store=new Store(':memory:'),slash=String.fromCharCode(92);
  assert.equal(rel(userDirectory(store,session(store,'ou_a',`a${slash}b:c/d..`))),'a_b_c_d');
  assert.equal(rel(userDirectory(store,session(store,'ou_b',''))),'ou_b');
  assert.equal(path.dirname(userDirectory(store,session(store,'ou_c','../../x'))),config.user);
  store.close();
});

test('files are exchanged in plain input/ and outputs/ folders of the chat folder, with no naming scheme',()=>{
  const chat=path.join(config.user,'..','codex-chat-folder');
  assert.equal(taskWorkspace(chat),path.join(chat,'outputs'));assert.equal(sessionInputs(chat),path.join(chat,'input'));
  assert.equal(fs.existsSync(taskWorkspace(chat)),false,'nothing is created on disk by asking for the path');
});

test('user data outlives a chat: a renewed session shares the same user directory',()=>{
  const store=new Store(':memory:'),first=session(store,'ou_1','李小明'),task=store.create({...input('ou_1'),source_message_id:'again'}).task;
  const before=userData(store,first);store.update(task.id,{status:'COMPLETED'});
  const renewed=store.renewSession(store.get(task.id),'archived');
  assert.notEqual(renewed.id,first.id);assert.equal(userData(store,store.session(renewed.id)),before);
  store.close();
});
