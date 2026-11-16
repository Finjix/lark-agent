import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {config} from '../dist/config.js';
import {declaredJobs,Jobs} from '../dist/jobs.js';
import {venvPython} from '../dist/agent/environment.js';

const until=async(check,ms=10000)=>{const end=Date.now()+ms;while(!check()){if(Date.now()>end)throw Error('timed out');await new Promise(r=>setTimeout(r,20));}};
async function jobsFile(declared){
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'lark-jobs-')),file=path.join(dir,'jobs.json');
  if(declared!==undefined)await fs.writeFile(file,typeof declared==='string'?declared:JSON.stringify(declared));
  return {dir,file};
}
/** A job running a small node script (`body`) in a folder of its own, which stands for the project. */
async function scriptJob(body,extra={}){
  const {dir}=await jobsFile();await fs.writeFile(path.join(dir,'job.mjs'),body);
  return {dir,job:{name:'sync',everyMinutes:60,timeoutMinutes:1,command:process.execPath,args:['job.mjs'],...extra}};
}

test('jobs.json lists the jobs; python is the project\'s Python and node the service\'s own, anything else is run as written',async()=>{
  assert.deepEqual(await declaredJobs((await jobsFile()).file),{jobs:[],invalid:[]},'no file, no jobs');
  const {file}=await jobsFile({jobs:[{name:'audit-rules-sync',everyMinutes:1440,command:'python',args:['skills/audit-auto/scripts/audit.py','rules','sync']},
    {name:'n',everyMinutes:60,command:'node'},{name:'x',everyMinutes:60,command:'C:/tools/x.exe',args:['a']}]});
  assert.deepEqual((await declaredJobs(file)).jobs,[{name:'audit-rules-sync',everyMinutes:1440,timeoutMinutes:30,command:venvPython(),args:['skills/audit-auto/scripts/audit.py','rules','sync']},
    {name:'n',everyMinutes:60,timeoutMinutes:30,command:process.execPath,args:[]},{name:'x',everyMinutes:60,timeoutMinutes:30,command:'C:/tools/x.exe',args:['a']}]);
  const shipped=await declaredJobs(config.jobs);
  assert.deepEqual([shipped.invalid,shipped.jobs.map(j=>j.name)],[[],['audit-rules-sync']],'the project\'s own jobs.json is valid');
});

test('a job that cannot run is left out with the reason, and the others still run; a broken file says so',async()=>{
  const {jobs,invalid}=await declaredJobs((await jobsFile({jobs:[
    {name:'ok',everyMinutes:5,command:'node'},{name:'too-often',everyMinutes:1,command:'node'},{name:'bad name!',everyMinutes:60,command:'node'},
    {name:'no-command',everyMinutes:60},{name:'ok',everyMinutes:60,command:'node'},{name:'args',everyMinutes:60,command:'node',args:[1]}]})).file);
  assert.deepEqual(jobs.map(j=>j.name),['ok']);
  assert.equal(invalid.length,5);assert.match(invalid.join('\n'),/too-often：everyMinutes/);assert.match(invalid.join('\n'),/同名/);
  assert.match((await declaredJobs((await jobsFile('{not json')).file)).invalid[0],/无法解析/);
});

test('a run is a process in the project folder, with what skills get and no user; a run still going is not started twice',async()=>{
  const saved={...process.env};
  try{
    process.env.LARK_AGENT_USER_DIR='D:\\someone';
    const {dir,job}=await scriptJob(`import fs from 'node:fs';await new Promise(r=>setTimeout(r,300));fs.appendFileSync('runs.txt',[process.cwd(),process.env.LARK_AGENT_SHARED_DIR,process.env.LARK_AGENT_USER_DIR??'-',process.env.PYTHONUTF8].join('|')+'\\n');console.log('done');`);
    const jobs=new Jobs(0,dir);
    const first=jobs.run(job),second=jobs.run(job);
    assert.equal(jobs.status()[0].running,true);
    await Promise.all([first,second]);
    const runs=(await fs.readFile(path.join(dir,'runs.txt'),'utf8')).trim().split('\n').map(r=>r.split('|'));
    assert.equal(runs.length,1,'once');assert.equal(path.resolve(runs[0][0]),path.resolve(dir),'in the project folder');
    assert.deepEqual(runs[0].slice(1),[config.shared,'-','1'],'the shared folder, no user');
    const [state]=jobs.status();
    assert.deepEqual([state.running,state.runs,state.lastExit,state.lastError],[false,1,0,undefined]);
    await jobs.run(job);assert.equal(jobs.status()[0].runs,2,'the next one runs');
  }finally{for(const key of Object.keys(process.env))if(!(key in saved))delete process.env[key];Object.assign(process.env,saved);}
});

test('a failing run, one that outlasts its time and one still going when the service stops are ended and say why',async()=>{
  const {dir,job:failing}=await scriptJob('process.exit(3);');
  const jobs=new Jobs(0,dir);await jobs.run(failing);
  assert.equal(jobs.status()[0].lastError,'退出码 3');
  await fs.writeFile(path.join(dir,'slow.mjs'),'setTimeout(()=>{},60000);');
  await jobs.run({...failing,name:'slow',args:['slow.mjs'],timeoutMinutes:0.005});
  assert.match(jobs.status().find(s=>s.name==='slow').lastError,/超过/);
  const run=jobs.run({...failing,name:'hanging',args:['slow.mjs']});
  await until(()=>jobs.status().find(s=>s.name==='hanging')?.running);
  await jobs.stop();await run;
  assert.equal(jobs.status().find(s=>s.name==='hanging').running,false);
  await jobs.run(failing);assert.equal(jobs.status()[0].runs,1,'nothing runs once stopped');
});

test('started jobs run a little after the start, then on their interval',async()=>{
  const {dir,job}=await scriptJob(`import fs from 'node:fs';fs.appendFileSync('runs.txt','x');`);
  const jobs=new Jobs(50,dir);jobs.start([job]);
  assert.equal(jobs.status()[0].runs,0,'not at once');
  await until(()=>jobs.status()[0].runs===1);
  await jobs.stop();
});
