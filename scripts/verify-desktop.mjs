import fs from 'node:fs/promises';
import path from 'node:path';
import {randomUUID} from 'node:crypto';
import {config} from '../dist/config.js';
import {Store} from '../dist/storage/database.js';
import {NativeDesktop} from '../dist/desktop/adapter.js';
import {TaskService} from '../dist/tasks/service.js';
import {Scheduler} from '../dist/tasks/scheduler.js';
import {terminal} from '../dist/types.js';
if(!process.argv.includes('--live'))throw Error('该测试会创建临时 Desktop 聊天并使用账号额度；明确传入 --live 后运行');
const resumeIndex=process.argv.indexOf('--resume'),resume=resumeIndex>=0?process.argv[resumeIndex+1]:undefined;
if(resume&&!/^[a-f0-9]{8}$/.test(resume))throw Error('Invalid verification run ID');
const count=process.argv.includes('--parallel')?5:1,run=resume??randomUUID().slice(0,8);
await fs.mkdir(config.data,{recursive:true});const store=new Store(path.join(config.data,`verify-${run}.sqlite`));
const desktop=new NativeDesktop(store),service=new TaskService(store,desktop),scheduler=new Scheduler(service);
const report={run,count,startedAt:new Date().toISOString(),success:false,maxRunning:0,maxCommands:0,maxPerUser:0,events:[],tasks:[]};
const tasks=[];let timer;
try{
  console.log(JSON.stringify({run,phase:'probe'}));
  await desktop.probe();
  console.log(JSON.stringify({run,phase:resume?'reconcile':'create'}));
  if(resume){
    for(const row of store.list()){const marker=row.instruction.match(/LARK_VERIFY_[a-f0-9]{8}_\d+/)?.[0];tasks.push({id:row.id,marker,user:row.user_id});}
    report.recoveredAfterRestart=true;await service.reconcile();
  }
  for(let i=0;!resume&&i<count;i++){
    const marker=`LARK_VERIFY_${run}_${i+1}`,user=['A','A','A','B','C'][i];
    const task=store.create({tenant_key:'verification',user_id:user,chat_id:`test-${user}`,source_message_id:`${run}-${i}`,instruction:
      `这是明确授权的桥接验收。只执行一次 PowerShell 命令 Start-Sleep -Seconds ${count===1?1:12}; Write-Output '${marker}'。不要访问其他文件或网络，不要创建子代理或聊天。先在进度消息说明正在进行验收，最终只回复 ${marker}。`}).task;
    tasks.push({id:task.id,marker,user});
  }
  timer=setInterval(()=>{
    const active=store.active();report.maxRunning=Math.max(report.maxRunning,active.filter(t=>t.status==='RUNNING').length);
    for(const user of new Set(active.map(t=>t.user_id)))report.maxPerUser=Math.max(report.maxPerUser,active.filter(t=>t.user_id===user).length);
    report.maxCommands=Math.max(report.maxCommands,active.filter(t=>t.progress.includes('commandExecution：inProgress')).length);
    for(const t of store.list()){
      const previous=report.events.findLast(e=>e.id===t.id);
      if(previous?.status!==t.status)report.events.push({id:t.id,status:t.status,at:Date.now()});
    }
  },50);
  scheduler.start();
  const deadline=Date.now()+240000;
  while(Date.now()<deadline){if(store.list().every(t=>terminal(t.status)))break;await new Promise(r=>setTimeout(r,250));}
  report.tasks=tasks.map(t=>{const row=store.get(t.id);return {...t,status:row.status,threadId:row.thread_id,turnId:row.turn_id,workspace:row.workspace_path,replyMatches:row.result.trim()===t.marker,error:row.error,startedAt:row.started_at,finishedAt:row.finished_at};});
  const sameUser=report.tasks.filter(t=>t.user==='A');report.fifoCorrect=sameUser.every((t,i)=>!i||t.startedAt>=sameUser[i-1].startedAt);
  report.success=report.tasks.every(t=>t.status==='COMPLETED'&&t.replyMatches)&&new Set(report.tasks.map(t=>t.threadId)).size===count&&report.fifoCorrect&&report.maxPerUser<=2&&(count===1||report.maxRunning===4&&report.maxCommands===4);
  if(!report.success)process.exitCode=1;
}catch(e){report.error=e.message;process.exitCode=1;}
finally{
  clearInterval(timer);await scheduler.stop();
  for(const t of store.list())if(t.thread_id&&terminal(t.status))try{await desktop.archive(t.thread_id);}catch{}
  desktop.close();store.close();report.finishedAt=new Date().toISOString();
  const file=path.join(config.data,`verify-${run}.json`);await fs.writeFile(file,JSON.stringify(report,null,2));console.log(JSON.stringify({...report,file},null,2));
}
