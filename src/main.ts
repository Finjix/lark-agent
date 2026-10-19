import http from 'node:http';
import fs from 'node:fs/promises';
import path from 'node:path';
import {randomBytes,timingSafeEqual} from 'node:crypto';
import {config,ensureDirectories,restrictSecret} from './config.js';
import {log,safeError} from './log.js';
import {Store} from './storage/database.js';
import {CodexRunner,codexCli} from './codex/runner.js';
import {FeishuClient} from './feishu/client.js';
import {TaskService} from './tasks/service.js';
import {Scheduler} from './tasks/scheduler.js';
import {Delivery} from './feishu/delivery.js';
import {cleanup} from './maintenance.js';
import {accessConfigured} from './access.js';
import {clockOffset} from './feishu/clock.js';

async function main(){
  ensureDirectories();const action=process.argv[2]??'start';
  if(action==='stop'){
    const token=await fs.readFile(path.join(config.data,'control.token'),'utf8');
    const response=await fetch(`http://127.0.0.1:${config.HEALTH_PORT}/shutdown`,{method:'POST',headers:{authorization:`Bearer ${token}`}});
    if(!response.ok)throw Error(`停止请求失败：${response.status}`);console.log('已请求安全停止');return;
  }
  // The Codex CLI of this project, with the project's own Codex home: `start.cmd codex login`, `start.cmd codex login status`, ...
  if(action==='codex'){process.exitCode=await codexCli(process.argv.slice(3));return;}
  const store=new Store(config.database),codex=new CodexRunner();
  if(action==='retry-delivery'){
    const id=process.argv[3];if(!id)throw Error('请指定任务 ID 或明确传入 all');
    const sql="UPDATE delivery_outbox SET status='PENDING',next_attempt_at=0,version=version+1 WHERE status IN ('FAILED','RETRYING')";
    const result=id==='all'?store.db.prepare(sql).run():store.db.prepare(sql+' AND task_id=?').run(id);
    store.close();console.log(`已恢复 ${result.changes} 条投递，不会重新执行任务`);return;
  }
  if(action==='backup'){
    const file=path.join(config.data,`backup-${Date.now()}.sqlite`);await store.db.backupTo(file);store.close();console.log(file);return;
  }
  if(action==='doctor'){
    const checks:any={runtime:{ok:Number(process.versions.node.split('.')[0])>=24,version:process.version,executable:process.execPath},
      sqlite:{ok:true},configuration:{credentials:!!(config.FEISHU_APP_ID&&config.FEISHU_APP_SECRET),allowedUsers:config.users.length,administrators:config.admins.length,allowedTenants:config.tenants.length}};
    await codex.probe();
    checks.codex={ready:codex.ready,version:codex.version,home:config.codexHome,login:config.CODEX_API_KEY?'API key（.env）':'codex login',model:config.CODEX_MODEL||'（Codex 默认）',...(codex.error?{error:codex.error}:{})};
    if(config.FEISHU_APP_ID&&config.FEISHU_APP_SECRET){try{const f=new FeishuClient();const info=await f.raw('GET','/open-apis/bot/v3/info');checks.feishu={ok:true,botName:info.bot?.app_name??info.bot?.name};}catch(e){checks.feishu={ok:false,error:safeError(e)};}}
    store.close();console.log(JSON.stringify(checks,null,2));
    if(!checks.runtime.ok||!codex.ready||!checks.configuration.credentials||!accessConfigured())process.exitCode=1;return;
  }
  if(action!=='start')throw Error('支持命令：start、codex、doctor、stop、backup、retry-delivery');
  // Task state lives in SQLite and is reconciled on every pass, so one stray error should be logged rather than take the service down.
  process.on('unhandledRejection',e=>log.error({error:safeError(e)},'未处理的异步错误'));
  process.on('uncaughtException',e=>log.error({error:safeError(e)},'未捕获的异常'));
  let stopping=false;let poll:NodeJS.Timeout|undefined,maintain:NodeJS.Timeout|undefined;
  let feishuStarted=false,lastFeishuAttempt=0,lastCodexProbe=0,lastCodexError='',lastClockSync=0;
  const token=randomBytes(32).toString('hex'),feishu=new FeishuClient();
  const service=new TaskService(store,codex,feishu),scheduler=new Scheduler(service),delivery=new Delivery(store,feishu);
  const server=http.createServer((request,response)=>{
    response.setHeader('Content-Type','application/json; charset=utf-8');response.setHeader('Cache-Control','no-store');
    if(request.headers.origin){response.writeHead(403).end('{}');return;}
    if(request.url==='/shutdown'&&request.method==='POST'){
      const given=Buffer.from(request.headers.authorization??''),expected=Buffer.from(`Bearer ${token}`);
      if(given.length!==expected.length||!timingSafeEqual(given,expected)){response.writeHead(403).end('{}');return;}
      response.end('{"stopping":true}');void stop();return;
    }
    if(request.method!=='GET'||!['/health','/ready'].includes(request.url??'')){response.writeHead(404).end('{}');return;}
    const ready=codex.ready&&feishu.connected&&accessConfigured();
    response.writeHead(request.url==='/ready'&&!ready?503:200).end(JSON.stringify({ready,access:{mode:config.tenants.length?'tenant':'users',tenantCount:config.tenants.length},codex:{ready:codex.ready,version:codex.version,error:codex.error},feishu:{connected:feishu.connected,state:feishu.state,error:feishu.lastError},...store.stats()}));
  });
  const stop=async()=>{
    if(stopping)return;stopping=true;clearInterval(poll);clearInterval(maintain);server.close();
    const forced=setTimeout(()=>process.exit(0),15000);forced.unref();
    // Codex runs as a child of this service and ends with it: the tasks are finished as failed, and one last delivery pass finishes their cards.
    service.stop();await scheduler.stop();await delivery.flush().catch(()=>{});await delivery.stop();
    feishu.close();codex.close();store.close();
    await fs.rm(path.join(config.data,'control.token'),{force:true});log.info('服务已安全停止');clearTimeout(forced);
  };
  try{await new Promise<void>((resolve,reject)=>{server.once('error',reject);server.listen(config.HEALTH_PORT,'127.0.0.1',resolve);});}
  catch(e){store.close();throw e;}
  await fs.writeFile(path.join(config.data,'control.token'),token,{mode:0o600});
  restrictSecret(path.join(config.data,'control.token'));
  process.once('SIGINT',()=>{void stop();});process.once('SIGTERM',()=>{void stop();});
  // Before anything can run or send: what the previous run left unfinished is over and must not reach users now.
  const leftovers=store.discardLeftovers();if(leftovers.cancelled||leftovers.dropped||leftovers.interrupted)log.info(leftovers,'已作废上次运行遗留的任务和未送达消息');
  scheduler.start();delivery.start();
  let polling=false;
  const reconnect=async()=>{
    if(polling||stopping)return;polling=true;
    try{
      if(Date.now()-lastClockSync>600000){
        lastClockSync=Date.now();
        void clockOffset().then(offset=>{service.clockOffsetMs=offset;if(Math.abs(offset)>5000)log.warn({offsetMs:Math.round(offset)},'本机时钟与飞书相差较大，已按飞书时间判断消息是否过期，请校准系统时间');}).catch(e=>log.debug({error:safeError(e)},'读取飞书时间失败'));
      }
      // Not ready: look again every few seconds, so a login done meanwhile is noticed. Ready: look now and then, so a login that expires or is removed is too.
      if(Date.now()-lastCodexProbe>(codex.ready?60000:10000)){
        lastCodexProbe=Date.now();const was=codex.ready;await codex.probe();
        if(codex.ready&&!was){log.info({version:codex.version,home:config.codexHome},'Codex 已就绪');lastCodexError='';}
        else if(!codex.ready&&codex.error!==lastCodexError){lastCodexError=codex.error;log.warn({error:codex.error},'等待 Codex 就绪');}
      }
      // The SDK gives up after a non-retryable error (for example rejected credentials); build a fresh client instead of staying deaf.
      if(feishuStarted&&feishu.state==='failed'){log.warn({error:feishu.lastError},'飞书长连接已失效，正在重建');feishu.close();feishuStarted=false;lastFeishuAttempt=0;}
      if(!feishuStarted&&Date.now()-lastFeishuAttempt>15000){lastFeishuAttempt=Date.now();
        try{await feishu.start(message=>service.receive(message));feishuStarted=true;log.info('飞书长连接已启动');}
        catch(e){feishu.close();feishu.lastError=safeError(e);log.warn({error:feishu.lastError},'等待飞书配置或连接');}}
    }finally{polling=false;}
  };
  poll=setInterval(()=>{void reconnect().catch(e=>log.error({error:safeError(e)},'恢复检查失败'));},5000);
  maintain=setInterval(()=>{void cleanup(store).catch(e=>log.warn({error:safeError(e)},'保留策略清理失败'));},3600000);
  log.info({root:config.root,health:`http://127.0.0.1:${config.HEALTH_PORT}/health`},'飞书 Codex 机器人已启动');
  if(!accessConfigured())log.warn('未配置开放企业或用户；任务执行关闭，可通过 /whoami 获取租户和 Open ID');
  await reconnect();
}
main().catch(e=>{log.error({error:safeError(e)},'启动失败');process.exitCode=1;});
