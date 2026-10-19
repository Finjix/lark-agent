import test from 'node:test';
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {Store} from '../dist/storage/database.js';
import {TaskService} from '../dist/tasks/service.js';
import {FeishuClient} from '../dist/feishu/client.js';
import {userDirectory} from '../dist/layout.js';
import {displayName} from '../dist/sessions/naming.js';

test('display names are single tidy lines of at most 80 characters',()=>{
  assert.equal(displayName(' 薯薯\n\t李 '),'薯薯 李');
  assert.equal(displayName('\x00\x07'),'');
  assert.equal([...displayName('🙂'.repeat(100))].length,80);
});

test('user names are looked up only for authorized tasks and name the user\'s folder; missing contact permissions never block the original request',async()=>{
  const s=new Store(':memory:'),lookups=[],jobs=[];
  const codex=Object.assign(new EventEmitter(),{ready:true,run:async function*(job){jobs.push(job);yield {type:'thread.started',thread_id:`th-${jobs.length}`};yield {type:'turn.completed',usage:{}};}});
  const service=new TaskService(s,codex,{getUserName:async user=>{lookups.push(user);if(user==='blocked')throw Error('contact scope missing');return '薯薯';}});
  const message={tenant:'tenant',user:'user',chat:'chat',message:'m',text:'  原文\n',isGroup:false};service.authorized=()=>false;await service.receive(message);assert.equal(lookups.length,0);
  service.authorized=()=>true;await service.receive(message);let task=s.list()[0];s.transition(task.id,'STARTING');await service.start(s.get(task.id));
  const named=s.session(s.get(task.id).session_id);assert.equal(named.display_name,'薯薯');assert.equal(jobs[0].input[0].text,message.text);
  assert.equal(userDirectory(s,named).split(/[\\/]/).at(-1),'薯薯');assert(named.workspace_path.includes('薯薯'),'Codex works in a folder named for the user');
  await service.receive({...message,user:'blocked',message:'blocked'});task=s.list()[1];s.transition(task.id,'STARTING');await service.start(s.get(task.id));
  assert.equal(jobs.length,2);assert.equal(s.get(task.id).status,'COMPLETED');assert.equal(s.session(s.get(task.id).session_id).display_name,null);s.close();
});

test('contact lookup caches only names and does not confuse an accessible user record with a returned name',async()=>{
  const client=new FeishuClient({appId:'test',appSecret:'test'}),calls=[];
  client.sdk.request=async request=>{calls.push(request);return {code:0,data:{user:request.url.includes('/missing?')?{open_id:'missing'}:{name:'薯薯',email:'do-not-cache'}}};};
  assert.equal(await client.getUserName('user'),'薯薯');assert.equal(await client.getUserName('user'),'薯薯');assert.equal(await client.getUserName('missing'),undefined);assert.equal(await client.getUserName('missing'),undefined);
  assert.equal(calls.length,2);assert.equal(calls[0].timeout,5000);assert(!JSON.stringify([...client.userNames.values()]).includes('do-not-cache'));client.close();
});
