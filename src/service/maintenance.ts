import fs from 'node:fs/promises';
import path from 'node:path';
import {spawn} from 'node:child_process';
import {loadSkillsFromDir} from '@earendil-works/pi-coding-agent';
import {config} from '../config.js';
import {skillEnv,systemPython,type SkillUser} from '../agent/environment.js';
import {Store} from '../storage/database.js';
import {log,safeError} from '../log.js';
import {removeInputs} from '../feishu/inputs.js';
import {workspaceIn} from '../users/layout.js';
import {endTree} from './jobs.js';

const DAY=86400000;
/** A file's age is its most recent creation, change or move (as in sweep): one a later task changed or delivered again is still in use. */
const untouched=async(file:string,cutoff:number)=>{
  const stat=await fs.lstat(file).catch(()=>undefined);
  return !stat||Math.max(stat.birthtimeMs,stat.mtimeMs,stat.ctimeMs)<cutoff;
};
/**
 * Removes one task's own files once the retention period is over: what it delivered and the attachments it received, unless someone has touched
 * them since (such a file is left to purge, which removes it once it too has been left alone that long). The workspace, the user's chat and other
 * tasks' files stay. A task whose folder is not the workspace of its session is skipped, and says so in the log: nothing is removed on a guess.
 */
export async function cleanup(store:Store,now=Date.now()){
  const cutoff=now-config.RETENTION_DAYS*DAY;
  for(const task of store.expired(cutoff)){
    if(store.db.prepare("SELECT 1 FROM delivery_outbox WHERE task_id=? AND status!='DELIVERED' LIMIT 1").get(task.id))continue;
    // Nothing of its own to remove (it never ran in a workspace and received nothing): done with, so it is not looked at again every day.
    if((!task.workspace_path||!task.thread_id)&&!store.inputs(task.id).length){store.update(task.id,{cleaned_at:Date.now()});continue;}
    try{
      const session=task.session_id?store.session(task.session_id):undefined;
      if(task.workspace_path&&task.thread_id){
        const target=path.resolve(task.workspace_path);let actual:string|undefined;
        // A directory that is already gone needs no removal, but the task must still be marked cleaned or it is retried (and logged) forever.
        try{actual=await fs.realpath(target);}catch(e:any){if(e.code!=='ENOENT')throw e;}
        if(actual){
          const sessionRoot=session?.workspace_path?await fs.realpath(session.workspace_path):'';
          // Both sides real paths (Windows: realpath also fixes the drive letter's case); the workspace itself must not be a link.
          if(!sessionRoot||actual!==sessionRoot||(await fs.lstat(target)).isSymbolicLink())throw Error('目录不满足会话任务所有权检查，跳过清理');
          // The workspace is shared by all of the user's tasks and stays; only the files this task delivered go.
          for(const {path:file} of store.db.prepare('SELECT path FROM task_artifacts WHERE task_id=?').all(task.id) as {path:string}[]){
            const rel=path.relative(actual,file);
            if(!rel||rel.startsWith('..')||path.isAbsolute(rel)||!await untouched(file,cutoff))continue;
            await fs.rm(file,{force:true});
          }
        }
      }
      if(session?.workspace_path){
        const received:string[]=[];
        for(const input of store.inputs(task.id))if(input.path&&await untouched(input.path,cutoff))received.push(input.path);
        await removeInputs(received,session.workspace_path);
      }
      store.update(task.id,{cleaned_at:Date.now()});store.event(task.id,'CLEANED',{});
    }catch(e){log.warn({taskId:task.id,error:safeError(e)},'保留策略清理已跳过');}
  }
}

/** What a sweep took away. */
export interface Removed {files:number;bytes:number}
const nothing=():Removed=>({files:0,bytes:0});
const addTo=(total:Removed,more:Removed)=>{total.files+=more.files;total.bytes+=more.bytes;};
/** A real folder. A link (symlink, junction) is not one: lstat does not follow it, so nothing outside the swept folder is reached through it. */
const plainDirectory=async(directory:string)=>(await fs.lstat(directory).catch(()=>undefined))?.isDirectory()===true;
/**
 * Deletes what under `root` nobody has touched since `cutoff`. A file's age is its most recent creation, change or move, so what is still
 * in use stays however long ago it was made; `only` limits the deletion to matching file names, and `shallow` to the files directly in `root`. A link is deleted as a link and never entered.
 * A folder left empty and created before the cutoff goes as well, `root` itself stays. What cannot be deleted now (open in a program,
 * no permission) is left for the next pass.
 */
async function sweep(root:string,cutoff:number,signal?:AbortSignal,only?:RegExp,shallow=false):Promise<Removed>{
  const removed=nothing();
  if(!await plainDirectory(root))return removed;
  const walk=async(directory:string)=>{
    for(const entry of await fs.readdir(directory,{withFileTypes:true})){
      if(signal?.aborted)return;
      const target=path.join(directory,entry.name);
      try{
        if(entry.isDirectory()){
          if(shallow)continue;
          await walk(target);
          // rmdir refuses a folder that still holds something, which is the answer.
          if((await fs.lstat(target)).birthtimeMs<cutoff)await fs.rmdir(target).catch(()=>{});
        }else if(!only||only.test(entry.name)){
          const stat=await fs.lstat(target);
          if(Math.max(stat.birthtimeMs,stat.mtimeMs,stat.ctimeMs)>=cutoff)continue;
          await fs.rm(target,{force:true});removed.files++;removed.bytes+=stat.isFile()?stat.size:0;
        }
      }catch(e){log.debug({path:target,error:safeError(e)},'清理时跳过一项');}
    }
  };
  try{await walk(root);}catch(e){log.debug({path:root,error:safeError(e)},'清理时无法读取目录');}
  return removed;
}

/**
 * What of pi's home may go once it is old, relative to it: the chats (a JSONL file each, written to with every turn, so one in use is never
 * old) and the crash records. The ChatGPT login (auth.json) and everything else stay.
 */
const AGENT_EXPENDABLE:{folder:string;only?:RegExp}[]=[{folder:'sessions',only:/\.jsonl$/i},{folder:'.',only:/^crashes\.json$/}];
/** `start.cmd backup` makes data/backup-<time>.sqlite: the newest few stay whatever their age, older ones go once untouched as long as anything. */
const BACKUPS_KEPT=3;
async function oldBackups(data:string,cutoff:number):Promise<Removed>{
  const removed=nothing(),stamp=(name:string)=>Number(name.slice('backup-'.length,-'.sqlite'.length));
  const backups=(await fs.readdir(data).catch(()=>[] as string[])).filter(name=>/^backup-\d+\.sqlite$/.test(name)).sort((a,b)=>stamp(b)-stamp(a));
  for(const name of backups.slice(BACKUPS_KEPT)){
    const file=path.join(data,name),stat=await fs.lstat(file).catch(()=>undefined);
    if(!stat?.isFile()||!await untouched(file,cutoff))continue;
    try{await fs.rm(file);removed.files++;removed.bytes+=stat.size;}catch(e){log.debug({path:file,error:safeError(e)},'清理时跳过一项');}
  }
  return removed;
}

/** The `prune` of a SKILL.md's frontmatter `metadata` (one level of YAML, a plain or quoted string), or undefined. */
export function pruneOf(skillMd:string):string|undefined{
  const lines=(skillMd.charCodeAt(0)===0xFEFF?skillMd.slice(1):skillMd).split(/\r?\n/);
  if(lines[0]?.trim()!=='---')return undefined;
  let inMetadata=false;
  for(const line of lines.slice(1)){
    if(line.trim()==='---')return undefined;
    if(/^\S/.test(line)){inMetadata=/^metadata:\s*$/.test(line);continue;}
    const found=inMetadata?/^\s+prune:\s*(.*?)\s*$/.exec(line):null;
    if(found)return found[1]!.replace(/^(["'])(.*)\1$/,'$2').trim()||undefined;
  }
  return undefined;
}
/**
 * A skill's own housekeeping: SKILL.md may name, in its frontmatter's `metadata`, a command that removes what the skill keeps that is
 * expendable (`prune: "scripts/<x>.py prune"`: a Python script inside the skill folder, and its arguments). It is run for every user directory
 * that holds a folder of that skill, with the user's environment (no chat), and answers with one line {"status":"ok","files","bytes"}. The
 * service knows nothing of what is inside a skill's folder: the skill decides what goes.
 */
export interface SkillPrune {skill:string;dir:string;script:string;args:string[]}
export async function skillPrunes(skills=config.skills):Promise<SkillPrune[]>{
  const found:SkillPrune[]=[];
  for(const skill of loadSkillsFromDir({dir:skills,source:'project'}).skills){
    const declared=pruneOf(await fs.readFile(skill.filePath,'utf8').catch(()=>''));
    if(!declared)continue;
    const [script='',...args]=declared.split(/\s+/).filter(Boolean);
    const file=path.resolve(skill.baseDir,script),rel=path.relative(skill.baseDir,file);
    if(!/\.py$/i.test(script)||!rel||rel.startsWith('..')||path.isAbsolute(rel)){log.warn({skill:skill.name,prune:declared},'技能的 prune 不是技能目录里的 Python 脚本，不运行');continue;}
    found.push({skill:skill.name,dir:skill.baseDir,script:file,args});
  }
  return found;
}
const PRUNE_TIMEOUT_MS=30*60_000;
/** One prune; on Windows its children outlive it unless the tree is ended, so a timeout or a stop ends the tree (as jobs do). */
function runPrune(prune:SkillPrune,user:SkillUser,signal?:AbortSignal):Promise<Removed>{
  return new Promise(done=>{
    if(signal?.aborted)return done(nothing());
    const child=spawn(systemPython(),[prune.script,...prune.args],{cwd:prune.dir,env:skillEnv(user),windowsHide:true,stdio:['ignore','pipe','pipe']});
    let stdout='',stderr='',why='';
    child.stdout!.on('data',chunk=>{stdout=(stdout+chunk.toString('utf8')).slice(-65536);});
    child.stderr!.on('data',chunk=>{stderr=(stderr+chunk.toString('utf8')).slice(-2000);});
    const stop=(reason:string)=>{why||=reason;endTree(child);};
    const timer=setTimeout(()=>stop(`超过 ${PRUNE_TIMEOUT_MS/60000} 分钟，已结束`),PRUNE_TIMEOUT_MS);timer.unref();
    const aborted=()=>stop('服务停止，已结束');signal?.addEventListener('abort',aborted,{once:true});
    const finish=(code:number|null,error?:string)=>{
      clearTimeout(timer);signal?.removeEventListener('abort',aborted);
      let answer:any;try{answer=JSON.parse(stdout.trim().split('\n').at(-1)?.trim()??'');}catch{}
      if(code!==0||answer?.status!=='ok'){
        if(!signal?.aborted)log.warn({skill:prune.skill,directory:user.dir,exit:code,error:error??(why||undefined),answer,stderr:stderr.slice(-500)},'技能的过期数据清理失败');
        return done(nothing());
      }
      done({files:Number(answer.files)||0,bytes:Number(answer.bytes)||0});
    };
    child.once('error',e=>finish(null,safeError(e)));
    child.once('close',code=>finish(code));
  });
}

export interface Purged {logs:Removed;backups:Removed;workspaces:Removed;scratch:Removed;skills:Record<string,Removed>;agent:Removed}
type Roots=Pick<typeof config,'data'|'user'|'agentHome'|'skills'>;
const usable=(name:string)=>!!name&&name!=='.'&&name!=='..'&&path.basename(name)===name;
/**
 * Deletes what has not been touched for RETENTION_DAYS: the service's own logs and old backups (the newest few stay), every file in the users'
 * workspaces (what the agent made, what the users sent), the agent's intermediate files in each user directory (tmp/), and the expendable files
 * of pi's home; and runs each skill's own housekeeping (SkillPrune) on the user directories that hold its data. The database, logins and
 * everything else stay. A user with a task queued or running keeps their directory untouched, and pi's files are left alone while any task runs.
 */
export async function purge(store:Store,options:{now?:number;roots?:Roots;signal?:AbortSignal;prunes?:SkillPrune[]}={}):Promise<Purged>{
  const {now=Date.now(),roots=config,signal}=options,cutoff=now-config.RETENTION_DAYS*DAY;
  const prunes=options.prunes??await skillPrunes(roots.skills);
  const purged:Purged={logs:nothing(),backups:nothing(),workspaces:nothing(),scratch:nothing(),skills:{},agent:nothing()};
  addTo(purged.logs,await sweep(path.join(roots.data,'logs'),cutoff,signal,/\.jsonl$/));
  addTo(purged.backups,await oldBackups(roots.data,cutoff));
  for(const {tenant_key,user_id,name} of store.userDirs()){
    if(signal?.aborted)return purged;
    if(!usable(name)||store.busy(tenant_key,user_id))continue;
    const directory=path.join(roots.user,name);
    if(!await plainDirectory(directory))continue;
    addTo(purged.workspaces,await sweep(workspaceIn(directory),cutoff,signal));
    addTo(purged.scratch,await sweep(path.join(directory,'tmp'),cutoff,signal));
    for(const prune of prunes){
      if(signal?.aborted)return purged;
      // Re-checked each time: a prune can take long, and a task the user starts meanwhile may be using the skill's data.
      if(store.busy(tenant_key,user_id)||!await plainDirectory(path.join(directory,prune.skill)))continue;
      addTo(purged.skills[prune.skill]??=nothing(),await runPrune(prune,{dir:directory,openId:user_id},signal));
    }
  }
  if(!store.active().length)for(const {folder,only} of AGENT_EXPENDABLE)addTo(purged.agent,await sweep(path.join(roots.agentHome,folder),cutoff,signal,only,folder==='.'));
  return purged;
}

let running:Promise<void>|undefined;
/** One round of housekeeping, run at start and then every 72 hours. It never rejects, and a round that is still running is not started a second time. */
export function housekeeping(store:Store,signal?:AbortSignal):Promise<void>{
  return running??=(async()=>{
    try{await cleanup(store);}catch(e){log.warn({error:safeError(e)},'保留策略清理失败');}
    try{
      const purged=await purge(store,{signal});
      const {skills,...own}=purged;
      if([...Object.values(own),...Object.values(skills)].some(x=>x.files))log.info(purged,'已清理超过保留期的数据');
    }catch(e){log.warn({error:safeError(e)},'过期数据清理失败');}
    // Task records are kept twice as long as files: by then cleanup and purge are long done with them.
    try{
      const forgotten=store.forget(Date.now()-2*config.RETENTION_DAYS*DAY);
      if(forgotten.tasks+forgotten.sessions+forgotten.notices)log.info(forgotten,'已清理过期的任务记录');
    }catch(e){log.warn({error:safeError(e)},'任务记录清理失败');}
  })().finally(()=>{running=undefined;});
}
