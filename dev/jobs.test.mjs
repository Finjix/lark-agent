import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {config} from '../dist/config.js';
import {declaredJobs,jobEnv,Jobs,JOBS_FILE} from '../dist/jobs.js';

const until=async(check,ms=10000)=>{const end=Date.now()+ms;while(!check()){if(Date.now()>end)throw Error('timed out');await new Promise(r=>setTimeout(r,20));}};
async function pluginWith(declared){
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'lark-jobs-'));
  if(declared!==undefined)await fs.writeFile(path.join(dir,JOBS_FILE),typeof declared==='string'?declared:JSON.stringify(declared));
  return {name:'audit-auto',path:dir};
}
/** A job running a small node script (`body`) in the plugin's folder. */
async function scriptJob(body,extra={}){
  const plugin=await pluginWith();await fs.writeFile(path.join(plugin.path,'job.mjs'),body);
  return {plugin:plugin.name,name:'sync',everyMinutes:60,timeoutMinutes:1,command:process.execPath,args:[path.join(plugin.path,'job.mjs')],cwd:plugin.path,...extra};
}

test('a plugin declares its periodic jobs in lark-agent.json; ${PLUGIN_ROOT} is its installed folder and node is the service\'s own',async()=>{
  assert.deepEqual(await declaredJobs(await pluginWith()),{jobs:[],invalid:[]},'no file, no jobs');
  const plugin=await pluginWith({jobs:[{name:'rules-sync',everyMinutes:60,command:'node',args:['${PLUGIN_ROOT}/scripts/rules.mjs','sync']}]});
  assert.deepEqual((await declaredJobs(plugin)).jobs,[{plugin:'audit-auto',name:'rules-sync',everyMinutes:60,timeoutMinutes:30,command:process.execPath,
    args:[path.normalize(`${plugin.path}/scripts/rules.mjs`),'sync'],cwd:plugin.path}]);
});

test('a job that cannot run is left out with the reason, and the others still run; a broken file says so',async()=>{
  const {jobs,invalid}=await declaredJobs(await pluginWith({jobs:[
    {name:'ok',everyMinutes:5,command:'node'},{name:'too-often',everyMinutes:1,command:'node'},{name:'bad name!',everyMinutes:60,command:'node'},
    {name:'no-command',everyMinutes:60},{name:'ok',everyMinutes:60,command:'node'},{name:'args',everyMinutes:60,command:'node',args:[1]}]}));
  assert.deepEqual(jobs.map(j=>j.name),['ok']);
  assert.equal(invalid.length,5);assert.match(invalid.join('\n'),/too-often：everyMinutes/);assert.match(invalid.join('\n'),/同名/);
  assert.match((await declaredJobs(await pluginWith('{not json'))).invalid[0],/无法解析/);
});

test('a job gets the bot\'s Feishu app, the shared folder and Codex\'s home, and no user: none is inherited from the service',()=>{
  const saved={...process.env},app={id:config.FEISHU_APP_ID,secret:config.FEISHU_APP_SECRET};
  try{
    Object.assign(process.env,{LARK_AGENT_USER_DIR:'D:\\someone',LARK_AGENT_CHAT_ID:'oc_someone',LARK_AGENT_SHARED_DIR:'D:\\elsewhere'});
    Object.assign(config,{FEISHU_APP_ID:'cli_bot',FEISHU_APP_SECRET:'bot-secret'});
    const env=jobEnv();
    assert.deepEqual([env.FEISHU_APP_ID,env.FEISHU_APP_SECRET,env.LARK_AGENT_SHARED_DIR,env.CODEX_HOME],['cli_bot','bot-secret',config.shared,config.codexHome]);
    assert.equal(env.LARK_AGENT_USER_DIR,undefined);assert.equal(env.LARK_AGENT_CHAT_ID,undefined);
  }finally{for(const key of Object.keys(process.env))if(!(key in saved))delete process.env[key];Object.assign(process.env,saved);config.FEISHU_APP_ID=app.id;config.FEISHU_APP_SECRET=app.secret;}
});

test('a run is a process in the plugin\'s folder; a run still going is not started twice; what it did is kept in its state',async()=>{
  const job=await scriptJob(`import fs from 'node:fs';await new Promise(r=>setTimeout(r,300));fs.appendFileSync('runs.txt',process.cwd()+'\\n');console.log('done');`);
  const jobs=new Jobs(0);
  const first=jobs.run(job),second=jobs.run(job);
  assert.equal(jobs.status()[0].running,true);
  await Promise.all([first,second]);
  const runs=(await fs.readFile(path.join(job.cwd,'runs.txt'),'utf8')).trim().split('\n');
  assert.deepEqual(runs.map(r=>path.resolve(r)),[path.resolve(job.cwd)],'once, in the plugin\'s folder');
  const [state]=jobs.status();
  assert.deepEqual([state.running,state.runs,state.lastExit,state.lastError],[false,1,0,undefined]);
  await jobs.run(job);assert.equal(jobs.status()[0].runs,2,'the next one runs');
});

test('a failing run, one that outlasts its time and one still going when the service stops are ended and say why',async()=>{
  const jobs=new Jobs(0);
  const failing=await scriptJob('process.exit(3);');await jobs.run(failing);
  assert.equal(jobs.status()[0].lastError,'退出码 3');
  const slow=await scriptJob('setTimeout(()=>{},60000);',{name:'slow',timeoutMinutes:0.005});await jobs.run(slow);
  assert.match(jobs.status().find(s=>s.name==='slow').lastError,/超过/);
  const hanging=await scriptJob('setTimeout(()=>{},60000);',{name:'hanging'});const run=jobs.run(hanging);
  await until(()=>jobs.status().find(s=>s.name==='hanging')?.running);
  await jobs.stop();await run;
  assert.equal(jobs.status().find(s=>s.name==='hanging').running,false);
  await jobs.run(failing);assert.equal(jobs.status()[0].runs,1,'nothing runs once stopped');
});

test('started jobs run a little after the start, then on their interval',async()=>{
  const job=await scriptJob(`import fs from 'node:fs';fs.appendFileSync('runs.txt','x');`);
  const jobs=new Jobs(50);jobs.start([job]);
  assert.equal(jobs.status()[0].runs,0,'not at once');
  await until(()=>jobs.status()[0].runs===1);
  await jobs.stop();
});
