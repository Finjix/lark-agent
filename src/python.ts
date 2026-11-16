import {execFile} from 'node:child_process';
import {createHash} from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';
import {promisify} from 'node:util';
import {config} from './config.js';
import {log} from './log.js';
import {venvPython} from './agent/environment.js';

const run=promisify(execFile);
const STAMP='lark-agent-requirements.sha256';
/** Every skill's requirements.txt, in a stable order. */
export async function requirementFiles(skills=config.skills):Promise<string[]>{
  const entries=await fs.readdir(skills,{withFileTypes:true}).catch(()=>[]);
  const files:string[]=[];
  for(const entry of entries.sort((a,b)=>a.name.localeCompare(b.name))){
    const file=path.join(skills,entry.name,'requirements.txt');
    if(entry.isDirectory()&&await fs.stat(file).then(s=>s.isFile(),()=>false))files.push(file);
  }
  return files;
}
const digest=async(files:string[])=>{
  const hash=createHash('sha256');
  for(const file of files)hash.update(`${path.basename(path.dirname(file))}\0`).update(await fs.readFile(file));
  return hash.digest('hex');
};
/** What Python and pip say, and whether the venv holds what the skills ask for now (for `doctor`). */
export async function pythonStatus(){
  const files=await requirementFiles();
  try{
    const version=(await run(venvPython(),['--version'],{windowsHide:true,timeout:20000})).stdout.trim();
    const stamp=await fs.readFile(path.join(config.venv,STAMP),'utf8').catch(()=>'');
    return {ok:true,version,venv:config.venv,requirements:files.map(f=>path.relative(config.root,f)),installed:stamp===await digest(files)};
  }catch(e){return {ok:false,venv:config.venv,error:`没有项目的 Python 环境（${(e as Error).message}），运行 start.cmd setup`};}
}
/**
 * Makes the project's Python environment (<project>/.venv) what the skills need: creates it with the machine's Python (3.11 or newer) when it
 * is missing, and installs every skill's requirements.txt when they changed since the last install. Quiet when nothing changed.
 */
export async function ensurePython(){
  const files=await requirementFiles(),wanted=await digest(files),stampFile=path.join(config.venv,STAMP);
  if(!await fs.stat(venvPython()).then(()=>true,()=>false)){
    log.info({venv:config.venv},'正在创建项目的 Python 环境');
    const python=process.env.PYTHON?.trim()||'python';
    await run(python,['-m','venv',config.venv],{windowsHide:true,timeout:300000});
  }
  if(await fs.readFile(stampFile,'utf8').catch(()=>'')===wanted)return;
  const {stdout}=await run(venvPython(),['-c','import sys;print(sys.version_info>=(3,11))'],{windowsHide:true,timeout:20000});
  if(stdout.trim()!=='True')throw Error('技能需要 Python 3.11 或更高版本：删除 .venv，装好新的 Python 后再运行 start.cmd setup');
  if(files.length){
    log.info({requirements:files.map(f=>path.relative(config.root,f))},'正在安装技能需要的 Python 包');
    await run(venvPython(),['-m','pip','install','--disable-pip-version-check','-q',...files.flatMap(f=>['-r',f])],{windowsHide:true,timeout:900000,maxBuffer:16*1024*1024});
  }
  await fs.writeFile(stampFile,wanted);
}
