import {execFile} from 'node:child_process';
import {createHash} from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';
import {promisify} from 'node:util';
import {config} from '../config.js';
import {log} from '../log.js';
import {systemPython} from '../agent/environment.js';

const run=promisify(execFile);
const STAMP='lark-agent-requirements.sha256';
/** The agent's own Feishu module's requirements.txt, then every skill's, in a stable order. */
export async function requirementFiles(skills=config.skills,feishu=path.dirname(config.feishu)):Promise<string[]>{
  const entries=await fs.readdir(skills,{withFileTypes:true}).catch(()=>[]);
  const files:string[]=[];
  const own=path.join(feishu,'requirements.txt');
  if(await fs.stat(own).then(s=>s.isFile(),()=>false))files.push(own);
  for(const entry of entries.sort((a,b)=>a.name.localeCompare(b.name))){
    const file=path.join(skills,entry.name,'requirements.txt');
    if(entry.isDirectory()&&await fs.stat(file).then(s=>s.isFile(),()=>false))files.push(file);
  }
  return files;
}
const STAMP_FILE=()=>path.join(config.data,STAMP);
const python=(args:string[],timeout=20000)=>run(systemPython(),args,{windowsHide:true,timeout,maxBuffer:16*1024*1024});
/** The requirements and the interpreter they were installed into: another Python, or a changed requirements.txt, means installing again. */
const digest=async(files:string[],executable:string)=>{
  const hash=createHash('sha256').update(`${executable.toLowerCase()}\0`);
  for(const file of files)hash.update(`${path.basename(path.dirname(file))}\0`).update(await fs.readFile(file));
  return hash.digest('hex');
};
/** Which Python `python` is, and whether it is new enough. */
async function interpreter(){
  const {stdout}=await python(['-c','import sys;print(sys.executable);print(sys.version.split()[0]);print(sys.version_info>=(3,11))']);
  const [executable='',version='',recent='']=stdout.trim().split(/\r?\n/);
  return {executable,version,recent:recent.trim()==='True'};
}
/** What the machine's Python is, and whether it holds what the skills ask for now (for `doctor`). */
export async function pythonStatus(){
  const files=await requirementFiles();
  try{
    const {executable,version,recent}=await interpreter();
    const stamp=await fs.readFile(STAMP_FILE(),'utf8').catch(()=>'');
    return {ok:recent,version,executable,requirements:files.map(f=>path.relative(config.root,f)),installed:stamp===await digest(files,executable),
      ...(recent?{}:{error:'技能需要 Python 3.11 或更高版本'})};
  }catch(e){return {ok:false,python:config.python,error:`找不到 Python（${(e as Error).message}）：装好 Python 3.11 或更高版本，或用环境变量 PYTHON 指定`};}
}
/**
 * Makes the machine's Python (`PYTHON`, or `python` on PATH; 3.11 or newer) hold what the Feishu module and the skills need: installs their requirements.txt
 * into it when they, or the Python, changed since the last install. Quiet when nothing changed.
 */
export async function ensurePython(){
  const files=await requirementFiles();
  const {executable,recent}=await interpreter().catch(e=>{throw Error(`找不到 Python（${(e as Error).message}）：装好 Python 3.11 或更高版本，或用环境变量 PYTHON 指定`);});
  if(!recent)throw Error(`技能需要 Python 3.11 或更高版本（现在是 ${executable}）`);
  const wanted=await digest(files,executable);
  if(await fs.readFile(STAMP_FILE(),'utf8').catch(()=>'')===wanted)return;
  if(files.length){
    log.info({python:executable,requirements:files.map(f=>path.relative(config.root,f))},'正在安装技能需要的 Python 包');
    // It can take minutes: without a line the console would look stuck.
    log.console('正在安装技能需要的 Python 包……');
    await python(['-m','pip','install','--disable-pip-version-check','-q',...files.flatMap(f=>['-r',f])],900000);
  }
  await fs.mkdir(config.data,{recursive:true});await fs.writeFile(STAMP_FILE(),wanted);
}
