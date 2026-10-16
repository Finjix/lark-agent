import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import {config} from '../dist/config.js';
import {Store} from '../dist/storage/database.js';
import {McpServer,identify,mcpToken} from '../dist/mcp/server.js';
import {registerMcp,unregisterMcp,mcpRegistered} from '../dist/mcp/register.js';
import {tools} from '../dist/mcp/tools.js';
import {toolInstructions} from '../dist/desktop/adapter.js';

const input=u=>({tenant_key:'t',user_id:u,chat_id:'c',source_message_id:`m-${u}`,instruction:'x'});
/** A user session bound to a Desktop thread with a task running in turn `turn`. */
function user(store,name,thread,turn='turn-1'){
  const task=store.create(input(name)).task,session=store.sessionFor(task);
  store.bindSession(session.id,thread,path.join(config.user,name));
  const workspace=path.join(config.user,name,'tasks',task.id);
  store.transition(task.id,'STARTING',{thread_id:thread,workspace_path:workspace,turn_id:turn});
  return {task,session:store.session(session.id),workspace};
}
const call=(thread,turn,name,args={})=>({name,arguments:args,_meta:{threadId:thread,'x-codex-turn-metadata':{thread_id:thread,turn_id:turn}}});

test('the caller is identified from Codex thread metadata, and anything else is refused',()=>{
  const store=new Store(':memory:'),a=user(store,'a','thread-a'),b=user(store,'b','thread-b');
  assert.equal(identify(store,call('thread-a','turn-1','x')).context.root,a.session.workspace_path);
  assert.equal(identify(store,call('thread-b','turn-1','x')).context.workspace,b.workspace);
  assert.match(identify(store,{name:'x',arguments:{}}).error,/线程标识/,'no metadata');
  assert.match(identify(store,{name:'x',arguments:{user:'b',key:'thread-b'}}).error,/线程标识/,'arguments cannot stand in for the metadata');
  assert.match(identify(store,call('thread-unknown','turn-1','x')).error,/不是飞书用户会话/);
  assert.match(identify(store,call('thread-a','turn-9','x')).error,/执行轮次/,'a call from another turn is refused');
  assert.match(identify(store,{_meta:{threadId:'thread-a','x-codex-turn-metadata':{thread_id:'thread-b'}}}).error,/不一致/);
  store.transition(a.task.id,'COMPLETED');
  assert.match(identify(store,call('thread-a','turn-1','x')).error,/没有正在执行/,'no running task, no tools');
  store.close();
});

test('MCP endpoint: auth, loopback only, initialize, tool list, and per-user tool calls',async t=>{
  const store=new Store(':memory:'),a=user(store,'a','thread-a'),b=user(store,'b','thread-b'),seen=[];
  const browsers={run:async(root,command,args)=>{seen.push({root,command,args});return `ok ${command}`;},close:async()=>{}};
  const server=new McpServer(store,browsers,{});
  const http_=http.createServer((q,r)=>void server.handle(q,r));await new Promise(r=>http_.listen(0,'127.0.0.1',r));t.after(()=>{http_.close();store.close();});
  const port=http_.address().port,token=mcpToken(store);
  const post=(body,headers={})=>fetch(`http://127.0.0.1:${port}/mcp`,{method:'POST',headers:{'content-type':'application/json',authorization:`Bearer ${token}`,...headers},body:JSON.stringify(body)});
  assert.equal((await post({jsonrpc:'2.0',id:1,method:'ping'},{authorization:'Bearer wrong'})).status,401);
  assert.equal((await post({jsonrpc:'2.0',id:1,method:'ping'},{origin:'http://evil.example'})).status,403);
  const rebinding=await new Promise(resolve=>{const q=http.request({host:'127.0.0.1',port,path:'/mcp',method:'POST',headers:{host:'evil.example',authorization:`Bearer ${token}`}},r=>{r.resume();resolve(r.statusCode);});q.end('{}');});
  assert.equal(rebinding,403,'a rebinding hostname is refused');
  assert.equal((await fetch(`http://127.0.0.1:${port}/mcp`,{headers:{authorization:`Bearer ${token}`}})).status,405);
  const init=await (await post({jsonrpc:'2.0',id:1,method:'initialize',params:{protocolVersion:'2025-06-18'}})).json();
  assert.equal(init.result.serverInfo.name,'lark-agent');assert.deepEqual(init.result.capabilities,{tools:{}});
  assert.equal((await post({jsonrpc:'2.0',method:'notifications/initialized'})).status,202);
  const list=await (await post({jsonrpc:'2.0',id:2,method:'tools/list'})).json();
  const names=list.result.tools.map(x=>x.name);
  assert(names.includes('browser_open')&&names.includes('browser_screenshot'));assert(list.result.tools.every(x=>x.inputSchema.type==='object'));
  const run=async(params)=>(await (await post({jsonrpc:'2.0',id:3,method:'tools/call',params})).json()).result;
  const ok=await run(call('thread-a','turn-1','browser_open',{url:'https://example.com'}));
  assert.equal(ok.isError,false);assert.equal(ok.content[0].text,'ok open');
  await run(call('thread-b','turn-1','browser_click',{target:'3'}));
  assert.deepEqual(seen.map(x=>[x.root,x.command]),[[a.session.workspace_path,'open'],[b.session.workspace_path,'click']],'each call lands in its own user\'s browser');
  const shot=await run(call('thread-a','turn-1','browser_screenshot',{name:'..\\..\\evil.png'}));
  assert.equal(shot.isError,false);const [file]=seen.at(-1).args;assert.equal(path.dirname(file),a.workspace,'screenshots go to the task directory whatever name is given');
  const denied=await run({name:'browser_open',arguments:{url:'https://example.com'},_meta:{}});
  assert.equal(denied.isError,true);assert.equal(seen.length,3,'a call without thread metadata never reaches the browser');
  const missing=await run(call('thread-a','turn-1','browser_open',{}));assert.equal(missing.isError,true);assert.match(missing.content[0].text,/url/);
  assert.equal((await run(call('thread-a','turn-1','nope'))).isError,true);
});

test('feishu_cli runs in the caller\'s own session with the task directory as working directory',async t=>{
  const saved={id:config.FEISHU_APP_ID,secret:config.FEISHU_APP_SECRET,cli:config.FEISHU_CLI_PATH};
  config.FEISHU_APP_ID='cli_test';config.FEISHU_APP_SECRET='s';config.FEISHU_CLI_PATH=process.execPath;
  const store=new Store(':memory:'),a=user(store,'a','thread-a');t.after(()=>{store.close();Object.assign(config,{FEISHU_APP_ID:saved.id,FEISHU_APP_SECRET:saved.secret,FEISHU_CLI_PATH:saved.cli});});
  const tool=tools.find(x=>x.name==='feishu_cli');assert(tool.enabled());
  const script=path.join(os.tmpdir(),`probe-${process.pid}.mjs`);await fs.writeFile(script,'console.log(JSON.stringify({cwd:process.cwd(),token:process.env.LARKSUITE_CLI_USER_ACCESS_TOKEN}))');t.after(()=>fs.rm(script,{force:true}));
  await fs.mkdir(path.join(a.session.workspace_path,'feishu-cli'),{recursive:true});await fs.mkdir(a.workspace,{recursive:true});
  await fs.writeFile(path.join(a.session.workspace_path,'feishu-cli','credentials.json'),JSON.stringify({accessToken:'u-a',expiresAt:Date.now()+3600000,savedAt:0}));
  const {FeishuCliAuth}=await import('../dist/feishu-cli/auth.js');
  const result=await tool.run({root:a.session.workspace_path,workspace:a.workspace,feishu:new FeishuCliAuth()},{args:[script]});
  assert.equal(result.isError,false);const out=JSON.parse(result.text);assert.equal(out.cwd,a.workspace);assert.equal(out.token,'u-a');
  await assert.rejects(tool.run({root:a.session.workspace_path,workspace:a.workspace,feishu:{}},{args:'docs'}),/args/);
});

test('registration writes one marked block into Codex config.toml and nothing else',async t=>{
  const home=await fs.mkdtemp(path.join(os.tmpdir(),'lark-codex-')),saved=config.codexHome;config.codexHome=home;
  t.after(async()=>{config.codexHome=saved;await fs.rm(home,{recursive:true,force:true});});
  const file=path.join(home,'config.toml'),original='model = "x"\n\n[mcp_servers.other]\ncommand = "node"\n';
  await fs.writeFile(file,original);
  assert.equal(registerMcp('tok1'),'registered');
  const written=await fs.readFile(file,'utf8');
  assert(written.startsWith(original)&&written.includes('[mcp_servers.lark_agent]')&&written.includes('Bearer tok1')&&written.includes('default_tools_approval_mode = "approve"'));
  assert.equal(await fs.readFile(`${file}.lark-agent.bak`,'utf8'),original,'the original is backed up once');
  assert.equal(registerMcp('tok1'),'unchanged');assert(mcpRegistered('tok1'));assert(!mcpRegistered('tok2'));
  await fs.writeFile(file,written.split('\n').join('\r\n'));assert.equal(registerMcp('tok1'),'unchanged','CRLF line endings do not cause rewrites');
  assert.equal(registerMcp('tok2'),'updated');assert.equal((await fs.readFile(file,'utf8')).split('lark_agent]').length,2,'still a single block');
  assert.equal(unregisterMcp(),'removed');assert.equal((await fs.readFile(file,'utf8')).replaceAll('\r\n','\n').trim(),original.trim());assert.equal(unregisterMcp(),'absent');
  await fs.writeFile(file,'[mcp_servers.lark_agent]\ncommand = "mine"\n');assert.throws(()=>registerMcp('t'),/手写/,'a hand-written entry is never overwritten');
});

test('instructions steer Codex to the MCP tools',()=>{
  const text=toolInstructions();assert(text.includes('lark_agent')&&text.includes('built-in browser')&&text.includes('browser_open'));
  assert(!/--key|lark-agent-browser/.test(text),'no per-session secret in the prompt');
});
