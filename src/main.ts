import http from 'node:http';
import fs from 'node:fs/promises';
import path from 'node:path';
import {randomBytes,timingSafeEqual} from 'node:crypto';
import {config,ensureDirectories,restrictSecret} from './config.js';
import {log,safeError} from './log.js';
import {Store} from './storage/database.js';
import {NativeDesktop} from './desktop/adapter.js';
import {FeishuClient} from './feishu/client.js';
import {TaskService} from './tasks/service.js';
import {Scheduler} from './tasks/scheduler.js';
import {Delivery} from './feishu/delivery.js';
import {cleanup} from './maintenance.js';

async function main(){
  ensureDirectories();const action=process.argv[2]??'start';
  if(action==='stop'){
    const token=await fs.readFile(path.join(config.data,'control.token'),'utf8');
    const response=await fetch(`http://127.0.0.1:${config.HEALTH_PORT}/shutdown`,{method:'POST',headers:{authorization:`Bearer ${token}`}});
    if(!response.ok)throw Error(`停止请求失败：${response.status}`);console.log('已请求安全停止');return;
  }
  const store=new Store(config.database),desktop=new NativeDesktop(store);
  if(action==='retry-delivery'){
    const id=process.argv[3];if(!id)throw Error('请指定任务 ID 或明确传入 all');
    const sql="UPDATE delivery_outbox SET status='PENDING',next_attempt_at=0,version=version+1 WHERE status IN ('FAILED','RETRYING')";
    const result=id==='all'?store.db.prepare(sql).run():store.db.prepare(sql+' AND task_id=?').run(id);
    store.close();console.log(`已恢复 ${result.changes} 条投递，不会重新执行任务`);return;
  }
  if(action==='backup'){
    const file=path.join(config.data,`backup-${Date.now()}.sqlite`);await store.db.backup(file);store.close();console.log(file);return;
  }
  if(action==='doctor'){
    const checks:any={runtime:{ok:process.version==='v24.21.0'&&process.arch==='x64',version:process.version,executable:process.execPath},
      sqlite:{ok:true},configuration:{credentials:!!(config.FEISHU_APP_ID&&config.FEISHU_APP_SECRET),allowedUsers:config.users.length,administrators:config.admins.length}};
    try{checks.desktop=await desktop.probe();}catch(e){checks.desktop={ready:false,error:safeError(e)};}
    if(config.FEISHU_APP_ID&&config.FEISHU_APP_SECRET){try{const f=new FeishuClient();const info=await f.raw('GET','/open-apis/bot/v3/info');checks.feishu={ok:true,botName:info.bot?.app_name??info.bot?.name};}catch(e){checks.feishu={ok:false,error:safeError(e)};}}
    desktop.close();store.close();console.log(JSON.stringify(checks,null,2));
    if(!checks.runtime.ok||!checks.desktop.ready||!checks.configuration.credentials||!(config.users.length+config.admins.length))process.exitCode=1;return;
  }
  if(action!=='start')throw Error('支持命令：start、doctor、stop、backup、retry-delivery');
  let stopping=false;let poll:NodeJS.Timeout|undefined,refresh:NodeJS.Timeout|undefined,maintain:NodeJS.Timeout|undefined;
  let feishuStarted=false,lastFeishuAttempt=0,lastReconcile=0;
  const token=randomBytes(32).toString('hex');
  const service=new TaskService(store,desktop),scheduler=new Scheduler(service),feishu=new FeishuClient(),delivery=new Delivery(store,feishu);
  const server=http.createServer((request,response)=>{
    response.setHeader('Content-Type','application/json; charset=utf-8');response.setHeader('Cache-Control','no-store');
    if(request.headers.origin){response.writeHead(403).end('{}');return;}
    if(request.url==='/shutdown'&&request.method==='POST'){
      const given=Buffer.from(request.headers.authorization??''),expected=Buffer.from(`Bearer ${token}`);
      if(given.length!==expected.length||!timingSafeEqual(given,expected)){response.writeHead(403).end('{}');return;}
      response.end('{"stopping":true}');void stop();return;
    }
    if(request.method!=='GET'||!['/health','/ready'].includes(request.url??'')){response.writeHead(404).end('{}');return;}
    const ready=desktop.ready&&feishu.connected&&(config.users.length+config.admins.length)>0;
    response.writeHead(request.url==='/ready'&&!ready?503:200).end(JSON.stringify({ready,desktop:{ready:desktop.ready,version:desktop.version,error:desktop.error},feishu:{connected:feishu.connected,error:feishu.lastError},...store.stats()}));
  });
  const stop=async()=>{
    if(stopping)return;stopping=true;clearInterval(poll);clearInterval(refresh);clearInterval(maintain);server.close();
    const forced=setTimeout(()=>process.exit(0),15000);forced.unref();
    desktop.close();await scheduler.stop();await delivery.stop();feishu.close();store.close();
    await fs.rm(path.join(config.data,'control.token'),{force:true});log.info('服务已安全停止');clearTimeout(forced);
  };
  try{await new Promise<void>((resolve,reject)=>{server.once('error',reject);server.listen(config.HEALTH_PORT,'127.0.0.1',resolve);});}
  catch(e){store.close();throw e;}
  await fs.writeFile(path.join(config.data,'control.token'),token,{mode:0o600});
  restrictSecret(path.join(config.data,'control.token'));
  process.once('SIGINT',()=>{void stop();});process.once('SIGTERM',()=>{void stop();});
  desktop.on('offline',()=>{for(const task of store.active())store.transition(task.id,'UNKNOWN',{error:'Desktop 连接中断，等待核对'});});
  scheduler.start();delivery.start();
  let polling=false;
  const reconnect=async()=>{
    if(polling||stopping)return;polling=true;
    try{
      if(!desktop.ready){try{await desktop.probe();await service.reconcile();lastReconcile=Date.now();}catch(e){log.warn({error:safeError(e)},'等待 Desktop 就绪');}}
      else if(Date.now()-lastReconcile>30000){await service.reconcile();lastReconcile=Date.now();}
      if(!feishuStarted&&Date.now()-lastFeishuAttempt>15000){lastFeishuAttempt=Date.now();
        try{await feishu.start(message=>service.receive(message));feishuStarted=true;log.info('飞书长连接已启动');}
        catch(e){feishu.close();feishu.lastError=safeError(e);log.warn({error:feishu.lastError},'等待飞书配置或连接');}}
    }finally{polling=false;}
  };
  poll=setInterval(()=>{void reconnect().catch(e=>log.error({error:safeError(e)},'恢复检查失败'));},5000);
  refresh=setInterval(()=>{if(desktop.ready)for(const task of store.active())if(task.thread_id)void desktop.readTask(task.thread_id).catch(()=>{});},3000);
  maintain=setInterval(()=>{if(desktop.ready)void cleanup(store,desktop);},3600000);
  log.info({root:config.root,health:`http://127.0.0.1:${config.HEALTH_PORT}/health`},'飞书 Desktop 机器人已启动');
  if(!config.users.length&&!config.admins.length)log.warn('ALLOWED_USERS 为空；任务执行关闭，可向机器人发送 /whoami 获取自己的 Open ID');
  await reconnect();
}
main().catch(e=>{log.error({error:safeError(e)},'启动失败');process.exitCode=1;});
