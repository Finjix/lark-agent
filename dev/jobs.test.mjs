import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {config} from '../dist/config.js';
import {declaredJobs,Jobs} from '../dist/service/jobs.js';
import {systemPython} from '../dist/agent/environment.js';

const until=async(check,ms=10000)=>{const end=Date.now()+ms;while(!check()){if(Date.now()>end)throw Error('timed out');await new Promise(r=>setTimeout(r,20));}};
/** A data/shared and a skills folder: `declared` maps a skill to what its jobs.json holds (a string is written as it is); every skill exists unless `missing`. */
async function folders(declared={},missing=[]){
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'lark-jobs-')),shared=path.join(dir,'shared'),skills=path.join(dir,'skills');
  for(const [skill,content] of Object.entries(declared)){
    await fs.mkdir(path.join(shared,skill),{recursive:true});if(!missing.includes(skill))await fs.mkdir(path.join(skills,skill),{recursive:true});
    if(content!==undefined)await fs.writeFile(path.join(shared,skill,'jobs.json'),typeof content==='string'?content:JSON.stringify(content));
  }
  return {dir,shared,skills};
}
/** A job running a small node script (`body`) in a folder of its own, which stands for the skill's folder. */
async function scriptJob(body,extra={}){
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'lark-job-'));await fs.writeFile(path.join(dir,'job.mjs'),body);
  return {dir,job:{name:'sync',everyMinutes:60,timeoutMinutes:1,command:process.execPath,args:['job.mjs'],cwd:dir,...extra}};
}

test('each skill\'s jobs.json in data/shared lists its jobs, run in the skill\'s folder; python is the machine\'s Python and node the service\'s own',async()=>{
  assert.deepEqual(await declaredJobs(path.join(os.tmpdir(),'no-such-shared-folder'),config.skills),{jobs:[],invalid:[]},'no folder, no jobs');
  const {shared,skills}=await folders({'audit-auto':{jobs:[{name:'audit-rules-sync',everyMinutes:1440,command:'python',args:['scripts/audit.py','rules','sync']}]},
    other:{jobs:[{name:'n',everyMinutes:60,command:'node'},{name:'x',everyMinutes:60,command:'C:/tools/x.exe',args:['a']}]},'no-jobs':undefined});
  assert.deepEqual((await declaredJobs(shared,skills)).jobs,[
    {name:'audit-rules-sync',everyMinutes:1440,timeoutMinutes:30,command:systemPython(),args:['scripts/audit.py','rules','sync'],cwd:path.join(skills,'audit-auto')},
    {name:'n',everyMinutes:60,timeoutMinutes:30,command:process.execPath,args:[],cwd:path.join(skills,'other')},
    {name:'x',everyMinutes:60,timeoutMinutes:30,command:'C:/tools/x.exe',args:['a'],cwd:path.join(skills,'other')}]);
});

test('a job that cannot run is left out with the reason, and the others still run; a broken file or a skill that is gone says so',async()=>{
  const {shared,skills}=await folders({a:{jobs:[
    {name:'ok',everyMinutes:5,command:'node'},{name:'too-often',everyMinutes:1,command:'node'},{name:'bad name!',everyMinutes:60,command:'node'},
    {name:'no-command',everyMinutes:60},{name:'args',everyMinutes:60,command:'node',args:[1]}]},b:{jobs:[{name:'ok',everyMinutes:60,command:'node'}]},
    broken:'{not json',gone:{jobs:[{name:'g',everyMinutes:60,command:'node'}]}},['gone']);
  const {jobs,invalid}=await declaredJobs(shared,skills),all=invalid.join('\n');
  assert.deepEqual(jobs.map(j=>j.name),['ok']);
  assert.equal(invalid.length,7);assert.match(all,/a\/jobs\.json 任务 too-often：everyMinutes/);assert.match(all,/b\/jobs\.json 任务 ok：.*同名/);
  assert.match(all,/broken\/jobs\.json 无法解析/);assert.match(all,/没有技能 gone/);
});

test('jobs declared later are scheduled, and ones no longer declared or changed stop being run on their old schedule',async()=>{
  const {job}=await scriptJob('');const jobs=new Jobs(60000);
  jobs.update([]);assert.deepEqual(jobs.status(),[]);
  jobs.update([job]);assert.deepEqual(jobs.status().map(s=>[s.name,s.everyMinutes]),[['sync',60]],'a job a skill wrote while the service runs');
  jobs.update([job]);assert.equal(jobs.status().length,1,'the same job is left as it is');
  jobs.update([{...job,everyMinutes:30}]);assert.deepEqual(jobs.status().map(s=>s.everyMinutes),[30],'a changed one starts over');
  jobs.update([]);assert.deepEqual(jobs.status(),[],'a removed one is gone');
  await jobs.stop();jobs.update([job]);assert.deepEqual(jobs.status(),[],'nothing is scheduled once stopped');
});

test('a run is a process in the folder of its skill, with what skills get and no user; a run still going is not started twice',async()=>{
  const saved={...process.env};
  try{
    process.env.LARK_AGENT_USER_DIR='D:\\someone';
    const {dir,job}=await scriptJob(`import fs from 'node:fs';await new Promise(r=>setTimeout(r,300));fs.appendFileSync('runs.txt',[process.cwd(),process.env.LARK_AGENT_SHARED_DIR,process.env.LARK_AGENT_USER_DIR??'-',process.env.PYTHONUTF8].join('|')+'\\n');console.log('done');`);
    const jobs=new Jobs(0);
    const first=jobs.run(job),second=jobs.run(job);
    assert.equal(jobs.status()[0].running,true);
    await Promise.all([first,second]);
    const runs=(await fs.readFile(path.join(dir,'runs.txt'),'utf8')).trim().split('\n').map(r=>r.split('|'));
    assert.equal(runs.length,1,'once');assert.equal(path.resolve(runs[0][0]),path.resolve(dir),'in the folder of the skill');
    assert.deepEqual(runs[0].slice(1),[config.shared,'-','1'],'the shared folder, no user');
    const [state]=jobs.status();
    assert.deepEqual([state.running,state.runs,state.lastExit,state.lastError],[false,1,0,undefined]);
    await jobs.run(job);assert.equal(jobs.status()[0].runs,2,'the next one runs');
  }finally{for(const key of Object.keys(process.env))if(!(key in saved))delete process.env[key];Object.assign(process.env,saved);}
});

test('a failing run, one that outlasts its time and one still going when the service stops are ended and say why',async()=>{
  const {dir,job:failing}=await scriptJob('process.exit(3);');
  const jobs=new Jobs(0);await jobs.run(failing);
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
  const jobs=new Jobs(50);jobs.start([job]);
  assert.equal(jobs.status()[0].runs,0,'not at once');
  await until(()=>jobs.status()[0].runs===1);
  await jobs.stop();
});
