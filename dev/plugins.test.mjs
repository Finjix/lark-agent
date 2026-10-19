import test,{after} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {config} from '../dist/config.js';
import {discover,fingerprint,mcpServers,syncPlugins,inspectPlugins,pluginConfig,MARKETPLACE,PLUGIN_ENV} from '../dist/plugins.js';
import {CodexRunner,codexEnv,codexConfig,codexOutput} from '../dist/codex/runner.js';

const SCHEMA='https://agent-plugins.org/schemas/1.0.0/plugin.schema.json';
const made=[];
const temp=async()=>{const directory=await fs.mkdtemp(path.join(os.tmpdir(),'lark-plugins-'));made.push(directory);return directory;};
after(async()=>{for(const directory of made)await fs.rm(directory,{recursive:true,force:true}).catch(()=>{});});
/** A plugin folder as a plugin author ships it: manifest, a skill and (optionally) a stdio MCP server. */
async function drop(root,folder,{name=folder,version='1.0.0',mcp=true,files={}}={}){
  const dir=path.join(root,folder);
  await fs.mkdir(path.join(dir,'skills','demo'),{recursive:true});
  await fs.writeFile(path.join(dir,'plugin.json'),JSON.stringify({$schema:SCHEMA,name,version}));
  await fs.writeFile(path.join(dir,'skills','demo','SKILL.md'),`---\nname: demo\ndescription: demo skill of ${name}\n---\nhello\n`);
  if(mcp)await fs.writeFile(path.join(dir,'mcp.json'),JSON.stringify({mcpServers:{[`${name}-tools`]:{type:'stdio',command:'node',args:['${PLUGIN_ROOT}/server/main.mjs','--flag'],cwd:'${PLUGIN_ROOT}',env:{STATIC:'${PLUGIN_ROOT}/x'}}}}));
  for(const [file,text] of Object.entries(files)){await fs.mkdir(path.dirname(path.join(dir,file)),{recursive:true});await fs.writeFile(path.join(dir,file),text);}
  return dir;
}

// ---- what counts as a plugin

test('a folder with a plugin.json is a plugin; dotted and underscored folders, loose files and broken ones are not, and each says why',async()=>{
  const root=await temp();
  await drop(root,'plugin-one');await drop(root,'second',{name:'second-name'});
  await drop(root,'.hidden');await drop(root,'_off');                                   // switched off by their names
  await fs.writeFile(path.join(root,'notes.txt'),'not a plugin');
  await fs.mkdir(path.join(root,'.agents','plugins'),{recursive:true});                     // the generated marketplace
  await fs.mkdir(path.join(root,'wrapped','plugin-one'),{recursive:true});await fs.writeFile(path.join(root,'wrapped','plugin-one','plugin.json'),'{"name":"x"}');
  await fs.mkdir(path.join(root,'broken'));await fs.writeFile(path.join(root,'broken','plugin.json'),'{oops');
  await drop(root,'badname',{name:'bad name@x'});await drop(root,'twin',{name:'second-name'});
  await fs.mkdir(path.join(root,'plain'));await fs.writeFile(path.join(root,'plain','plugin.json'),'{"name":"plain","version":"1.0.0"}');   // no $schema: Codex does not see it
  await fs.mkdir(path.join(root,'native','.codex-plugin'),{recursive:true});await fs.writeFile(path.join(root,'native','.codex-plugin','plugin.json'),'{"name":"native"}');
  const {found,skipped}=await discover(root);
  assert.deepEqual(found,[{name:'native',version:'',folder:'native'},{name:'plugin-one',version:'1.0.0',folder:'plugin-one'},{name:'second-name',version:'1.0.0',folder:'second'}]);
  const why=Object.fromEntries(skipped.map(s=>[s.folder,s.reason]));
  assert.deepEqual(Object.keys(why).sort(),['badname','broken','plain','twin','wrapped']);assert.match(why.plain,/没有 [$]schema/);
  assert.match(why.wrapped,/缺少 plugin\.json.*多套一层/);assert.match(why.broken,/无法解析/);assert.match(why.badname,/name/);assert.match(why.twin,/同名/);
  assert.deepEqual(await discover(path.join(root,'missing')),{found:[],skipped:[]},'no plugins folder is no plugins');
});

test('the fingerprint changes with any file, its contents, a rename or a new folder, and with nothing else',async()=>{
  const root=await temp(),dir=await drop(root,'p',{files:{'a/b.txt':'one'}});
  const base=await fingerprint(dir);assert.match(base,/^[0-9a-f]{64}$/);
  assert.equal(await fingerprint(dir),base,'stable');
  await fs.cp(dir,path.join(root,'copy'),{recursive:true});assert.equal(await fingerprint(path.join(root,'copy')),base,'the same content elsewhere is the same plugin');
  await fs.writeFile(path.join(dir,'a','b.txt'),'two');const edited=await fingerprint(dir);assert.notEqual(edited,base);
  await fs.rename(path.join(dir,'a','b.txt'),path.join(dir,'a','c.txt'));const renamed=await fingerprint(dir);assert.notEqual(renamed,edited);
  await fs.mkdir(path.join(dir,'empty'));assert.notEqual(await fingerprint(dir),renamed);
});

test('a plugin\'s stdio MCP servers are read with ${PLUGIN_ROOT} resolved, and run by the node that runs the service',async()=>{
  const root=await temp(),dir=await drop(root,'p',{mcp:false});
  assert.deepEqual(await mcpServers(dir),{},'no mcp.json, no servers');
  await fs.writeFile(path.join(dir,'mcp.json'),JSON.stringify({mcpServers:{
    a:{type:'stdio',command:'node',args:['${PLUGIN_ROOT}/s/a.mjs','plain'],cwd:'${PLUGIN_ROOT}',env:{K:'${PLUGIN_ROOT}/k'}},
    b:{command:'python',args:['b.py']},
    remote:{type:'http',url:'https://example.test/mcp'},broken:{args:['x']}}}));
  const servers=await mcpServers(dir);
  assert.deepEqual(Object.keys(servers),['a','b'],'only stdio servers with a command; the others stay as the plugin runs them');
  assert.equal(servers.a.command,process.execPath);assert.deepEqual(servers.a.args,[path.join(dir,'s','a.mjs'),'plain']);
  assert.equal(servers.a.cwd,dir);assert.deepEqual(servers.a.env,{K:path.join(dir,'k')});
  assert.deepEqual(servers.b,{command:'python',args:['b.py'],env:{}});
});

// ---- what a turn is given

const plugin=(name,servers)=>({id:`${name}@${MARKETPLACE}`,name,version:'1.0.0',path:`C:/codex/${name}`,mcp:Object.fromEntries(servers.map(s=>[s,{command:'node',args:['C:/codex/x.mjs'],env:{}}]))});

test('with plugins, Codex runs with its plugin feature on, each plugin\'s own MCP servers off, and the host\'s copy of them forwarding the user\'s environment',()=>{
  assert.deepEqual(pluginConfig([]),{},'no plugins, no change to what Codex is configured with');
  assert.equal(codexConfig().features.plugins,false);assert.deepEqual(Object.keys(codexConfig()).sort(),['cli_auth_credentials_store','features']);
  const plugins=[plugin('plugin-one',['feishu-auth']),plugin('other',['feishu-auth','my.tools']),plugin('no-servers',[])];
  const given=codexConfig(plugins);
  assert.equal(given.features.plugins,true);for(const f of ['apps','memories'])assert.equal(given.features[f],false,f);
  // The plugin id has an @ and must carry its own quotes: the SDK joins the segments of a path as they are.
  assert.deepEqual(given.plugins,{'"plugin-one@lark-agent"':{mcp_servers:{'feishu-auth':{enabled:false}}},'"other@lark-agent"':{mcp_servers:{'"my.tools"':{enabled:false}}}});
  assert.deepEqual(Object.keys(given.mcp_servers).sort(),['"my.tools"','feishu-auth'],'one server of a name: the first plugin keeps it');
  assert.deepEqual(given.mcp_servers['feishu-auth'].env_vars,[...PLUGIN_ENV],'names only, never values');
  assert.deepEqual(PLUGIN_ENV,['LARK_AGENT_USER_DIR','LARK_AGENT_USER_OPEN_ID','FEISHU_APP_ID','FEISHU_APP_SECRET']);
  assert(!JSON.stringify(given).includes('secret'));
});

test('a user\'s turn carries their folder, their Open ID and the bot\'s Feishu app; nothing else ever does',()=>{
  const saved={...process.env},app={id:config.FEISHU_APP_ID,secret:config.FEISHU_APP_SECRET};
  try{
    process.env.LARK_AGENT_USER_DIR='D:\\someone-else';process.env.LARK_AGENT_USER_OPEN_ID='ou_someone_else';process.env.FEISHU_APP_SECRET='from-the-service-env';
    Object.assign(config,{FEISHU_APP_ID:'cli_bot',FEISHU_APP_SECRET:'bot-secret'});
    const plain=codexEnv();
    for(const name of PLUGIN_ENV.filter(n=>n!=='FEISHU_APP_ID'))assert.equal(plain[name],undefined,`${name} is not inherited from the service`);
    const env=codexEnv({dir:'D:\\project\\user\\李小明',openId:'ou_1'});
    assert.deepEqual(PLUGIN_ENV.map(n=>env[n]),['D:\\project\\user\\李小明','ou_1','cli_bot','bot-secret']);
    assert.equal(env.CODEX_HOME,config.codexHome);
    config.FEISHU_APP_SECRET='';assert.equal('FEISHU_APP_SECRET' in codexEnv({dir:'D:\\u',openId:'ou_1'}),false,'what is not set is not passed empty');
  }finally{for(const key of Object.keys(process.env))if(!(key in saved))delete process.env[key];Object.assign(process.env,saved);config.FEISHU_APP_ID=app.id;config.FEISHU_APP_SECRET=app.secret;}
});

test('the runner gives a turn the plugins and the user\'s environment, and without plugins behaves as before',async()=>{
  const made=[];
  const factory=options=>{made.push(options);return {startThread:()=>({runStreamed:async()=>({events:(async function*(){})()})}),resumeThread:()=>{throw Error('unused');}};};
  const runner=new CodexRunner(factory),job={threadId:null,cwd:'D:/w',input:[],signal:new AbortController().signal,user:{dir:'D:\\u',openId:'ou_1'}};
  for await(const _ of runner.run(job));
  assert.equal(made[0].env.LARK_AGENT_USER_DIR,undefined);assert.deepEqual(made[0].config,codexConfig());
  runner.plugins=[plugin('plugin-one',['feishu-auth'])];
  for await(const _ of runner.run(job));
  assert.equal(made[1].env.LARK_AGENT_USER_DIR,'D:\\u');assert.equal(made[1].env.LARK_AGENT_USER_OPEN_ID,'ou_1');assert.equal(made[1].config.features.plugins,true);
  for await(const _ of runner.run({...job,user:undefined}));
  assert.equal(made[2].env.LARK_AGENT_USER_DIR,undefined,'a turn that is not a user\'s gets no user');
});

// ---- installing, with the real Codex CLI in a Codex home of its own

/** The project's Codex CLI against a throw-away Codex home, recording every command it is given. */
const cliIn=(home,calls=[])=>Object.assign(args=>{calls.push(args.slice(0,3).join(' '));return codexOutput(args,{...codexEnv(),CODEX_HOME:home});},{calls});
const installed=async cli=>JSON.parse(await cli(['plugin','list','--json'])).installed.map(p=>p.pluginId).sort();

test('plugins dropped into the folder are installed, left alone while unchanged, replaced when changed and removed when gone',{timeout:180000},async()=>{
  const base=await temp(),root=path.join(base,'plugins'),home=path.join(base,'home');
  await fs.mkdir(root);await fs.mkdir(home);
  const cli=cliIn(home);
  // Nothing dropped, nothing ever installed: Codex is not even started.
  assert.deepEqual(await syncPlugins(cli,{root,home}),{plugins:[],skipped:[]});assert.deepEqual(cli.calls,[]);

  const dir=await drop(root,'plugin-one',{files:{'skills/demo/old.md':'old'}});await drop(root,'second');
  await fs.mkdir(path.join(root,'broken'));await fs.writeFile(path.join(root,'broken','plugin.json'),'nope');
  assert.deepEqual((await inspectPlugins({root,home})).map(p=>[p.folder,p.state]),[['broken','invalid'],['plugin-one','not installed'],['second','not installed']]);
  let result=await syncPlugins(cli,{root,home});
  assert.deepEqual(result.plugins.map(p=>p.id),['plugin-one@lark-agent','second@lark-agent'],'the broken one does not hold the others back');
  assert.deepEqual(result.skipped.map(s=>s.folder),['broken']);
  assert.deepEqual(await installed(cli),['plugin-one@lark-agent','second@lark-agent']);
  const first=result.plugins[0];
  assert(first.path.toLowerCase().startsWith(path.join(home,'plugins','cache').toLowerCase()),'installed inside the Codex home');
  assert.equal((await fs.readFile(path.join(first.path,'skills','demo','SKILL.md'),'utf8')).includes('demo skill of plugin-one'),true);
  assert.deepEqual(first.mcp['plugin-one-tools'].args,[path.join(first.path,'server','main.mjs'),'--flag'],'resolved to the installed copy');
  const marketplace=JSON.parse(await fs.readFile(path.join(root,'.agents','plugins','marketplace.json'),'utf8'));
  assert.equal(marketplace.name,MARKETPLACE);assert.deepEqual(marketplace.plugins.map(p=>p.source.path),['./plugin-one','./second']);
  assert.deepEqual((await inspectPlugins({root,home})).map(p=>[p.folder,p.state]),[['broken','invalid'],['plugin-one','installed'],['second','installed']]);

  // Unchanged: no plugin is installed again.
  cli.calls.length=0;result=await syncPlugins(cli,{root,home});
  assert.deepEqual(result.plugins.map(p=>p.id),['plugin-one@lark-agent','second@lark-agent']);assert.equal(cli.calls.some(c=>c.startsWith('plugin add')),false,cli.calls.join(' | '));

  // Changed under the same version (a folder dropped over the old one, a file gone): the installed copy is replaced as a whole.
  await fs.rm(path.join(dir,'skills','demo','old.md'));await fs.writeFile(path.join(dir,'skills','demo','SKILL.md'),'---\nname: demo\ndescription: changed\n---\n');
  assert.equal((await inspectPlugins({root,home})).find(p=>p.folder==='plugin-one').state,'changed');
  cli.calls.length=0;result=await syncPlugins(cli,{root,home});
  assert.deepEqual(cli.calls.filter(c=>c.startsWith('plugin add')),['plugin add plugin-one@lark-agent'],'only the one that changed');
  const copy=result.plugins.find(p=>p.name==='plugin-one').path;
  assert.match(await fs.readFile(path.join(copy,'skills','demo','SKILL.md'),'utf8'),/changed/);
  await assert.rejects(fs.stat(path.join(copy,'skills','demo','old.md')),'what is gone from the folder is gone from the installed copy');

  // A new version lands next to nothing: Codex keeps one copy.
  await fs.writeFile(path.join(dir,'plugin.json'),JSON.stringify({$schema:SCHEMA,name:'plugin-one',version:'1.1.0'}));
  result=await syncPlugins(cli,{root,home});assert.equal(result.plugins.find(p=>p.name==='plugin-one').version,'1.1.0');
  assert.deepEqual(await fs.readdir(path.join(home,'plugins','cache',MARKETPLACE,'plugin-one')),['1.1.0']);

  // Deleting the folder uninstalls the plugin.
  await fs.rm(path.join(root,'second'),{recursive:true});
  result=await syncPlugins(cli,{root,home});
  assert.deepEqual(result.plugins.map(p=>p.id),['plugin-one@lark-agent']);assert.deepEqual(await installed(cli),['plugin-one@lark-agent']);
  await fs.rm(path.join(root,'plugin-one'),{recursive:true});
  result=await syncPlugins(cli,{root,home});assert.deepEqual(result.plugins,[]);assert.deepEqual(await installed(cli),[]);
  assert.deepEqual(JSON.parse(await fs.readFile(path.join(home,'lark-agent-plugins.json'),'utf8')),{},'nothing is remembered about what is gone');
  assert.deepEqual(await syncPlugins(cli,{root,home}),{plugins:[],skipped:[{folder:'broken',reason:result.skipped[0].reason}]},'still reports the broken folder');
});

test('when the project moves, the marketplace follows it and everything is installed again; a Codex that cannot be run costs only the plugins',{timeout:180000},async()=>{
  const base=await temp(),home=path.join(base,'home');await fs.mkdir(home);
  const cli=cliIn(home),before=path.join(base,'a','plugins'),after=path.join(base,'b','plugins');
  await fs.mkdir(before,{recursive:true});await drop(before,'p');
  assert.equal((await syncPlugins(cli,{root:before,home})).plugins.length,1);
  await fs.cp(before,after,{recursive:true});
  cli.calls.length=0;const moved=await syncPlugins(cli,{root:after,home});
  assert.equal(moved.plugins.length,1);assert(cli.calls.includes('plugin add p@lark-agent'),'installed again from the new place');
  const known=JSON.parse(await cli(['plugin','marketplace','list','--json'])).marketplaces;
  assert.deepEqual(known.map(m=>[m.name,path.resolve(m.root).toLowerCase()]),[[MARKETPLACE,path.resolve(after).toLowerCase()]]);
  const broken=await syncPlugins(async()=>{throw Error('codex 无法运行');},{root:after,home:path.join(base,'other-home')});
  assert.deepEqual(broken.plugins,[]);assert.match(broken.skipped[0].reason,/插件同步失败.*codex 无法运行/);
});
