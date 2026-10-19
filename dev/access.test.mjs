import test from 'node:test';
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {authorized,accessConfigured} from '../dist/access.js';
import {Store} from '../dist/storage/database.js';
import {TaskService} from '../dist/tasks/service.js';

const policy={tenants:['company'],users:['listed-user'],admins:['admin'],chats:[]};
const message=(user='new-user',tenant='company',chat='private-chat')=>({tenant,user,chat,message:`${tenant}-${user}-${chat}`,text:'hi',isGroup:false});

test('tenant access allows unlisted enterprise users in private chats and groups without enrollment',()=>{
  assert(authorized(message(),policy));assert(authorized({...message('other-user','company','group'),isGroup:true},policy));
  assert(accessConfigured({...policy,users:[],admins:[]}));
});

test('tenant boundary rejects external users and admins and never admits missing identity',()=>{
  for(const user of ['new-user','listed-user','admin'])assert.equal(authorized(message(user,'external-company'),policy),false);
  assert.equal(authorized(message(''),policy),false);assert.equal(authorized(message('new-user',''),policy),false);
  assert.equal(authorized(message(),{...policy,chats:['selected-group']}),false);
  assert(authorized(message('new-user','company','selected-group'),{...policy,chats:['selected-group']}));
});

test('legacy user allowlist remains available and empty access configuration denies everyone',()=>{
  const legacy={...policy,tenants:[]};assert(authorized(message('listed-user'),legacy));assert(authorized(message('admin'),legacy));
  assert.equal(authorized(message(),legacy),false);
  const empty={tenants:[],users:[],admins:[],chats:[]};assert.equal(accessConfigured(empty),false);assert.equal(authorized(message(),empty),false);
});

test('new enterprise members can create tasks; anybody else is refused, and told who they are so an administrator can add them',async()=>{
  const store=new Store(':memory:'),service=new TaskService(store,new EventEmitter());service.authorized=m=>authorized(m,policy);
  await service.receive(message('first-member'));await service.receive(message('second-member'));
  assert.equal(store.list().length,2);
  const [first,second]=store.list();assert.notEqual(store.sessionFor(first).id,store.sessionFor(second).id);
  await service.receive(message('external-member','external-company'));assert.equal(store.list().length,2);
  const [refusal]=store.due().filter(r=>r.kind==='NOTICE').map(r=>JSON.parse(r.payload).text);assert(refusal.includes('external-member')&&refusal.includes('external-company'));store.close();
});
