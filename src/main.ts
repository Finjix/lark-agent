import http from 'node:http';
import fs from 'node:fs/promises';
import {existsSync} from 'node:fs';
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
import {accessConfigured} from './access.js';
import {BrowserManager} from './browser/manager.js';
import {McpServer,mcpToken} from './mcp/server.js';
import {registerMcp,unregisterMcp,mcpRegistered} from './mcp/register.js';
import {setPrompt,removePrompt,promptCurrent} from './prompt.js';
import {feishuCliBinary} from './feishu-cli/run.js';
import {FeishuCliAuth} from './feishu-cli/auth.js';

async function main(){
  ensureDirectories();const action=process.argv[2]??'start';
  if(action==='stop'){
    const token=await fs.readFile(path.join(config.data,'control.token'),'utf8');
    const response=await fetch(`http://127.0.0.1:${config.HEALTH_PORT}/shutdown`,{method:'POST',headers:{authorization:`Bearer ${token}`}});
    if(!response.ok)throw Error(`停止请求失败：${response.status}`);console.log('已请求安全停止');return;
  }
  if(action==='set-prompt'){
    // Writes prompt/system.md into Codex's global instructions as one marked block; `set-prompt remove` takes it out again.
    if(process.argv[3]==='remove'){console.log(`Codex 系统提示词：${removePrompt()}`);return;}
    console.log(`Codex 系统提示词：${setPrompt()}（${path.join(config.codexHome,'AGENTS.md')}）\n来源：prompt/system.md，修改后再运行一次即可更新。`);return;
  }
  if(action==='register-mcp'){
    // The only place that edits Codex's config.toml: explicit, idempotent, and reversible with `register-mcp remove`.
    if(process.argv[3]==='remove'){console.log(`Codex 配置：MCP 服务器 ${unregisterMcp()}，重启 Codex Desktop 后生效`);return;}
    const store=new Store(config.database);
    try{console.log(`Codex 配置：MCP 服务器 ${registerMcp(mcpToken(store))}（${path.join(config.codexHome,'config.toml')}）\n需重启 Codex Desktop 后才会加载；飞书机器人服务运行时工具才可用。`);}finally{store.close();}
    return;
  }
  const store=new Store(config.database),desktop=new NativeDesktop(store);
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
      sqlite:{ok:true},tools:{feishuCli:existsSync(feishuCliBinary()),playwright:existsSync(path.join(config.root,'node_modules','playwright-core'))},prompt:{set:promptCurrent()},mcp:{registered:mcpRegistered(mcpToken(store)),url:`http://127.0.0.1:${config.HEALTH_PORT}/mcp`},configuration:{credentials:!!(config.FEISHU_APP_ID&&config.FEISHU_APP_SECRET),allowedUsers:config.users.length,administrators:config.admins.length,allowedTenants:config.tenants.length}};
    try{checks.desktop=await desktop.probe();}catch(e){checks.desktop={ready:false,error:safeError(e)};}
    if(config.FEISHU_APP_ID&&config.FEISHU_APP_SECRET){try{const f=new FeishuClient();const info=await f.raw('GET','/open-apis/bot/v3/info');checks.feishu={ok:true,botName:info.bot?.app_name??info.bot?.name};}catch(e){checks.feishu={ok:false,error:safeError(e)};}}
    desktop.close();store.close();console.log(JSON.stringify(checks,null,2));
    const toolsOk=checks.tools.feishuCli&&checks.tools.playwright&&checks.mcp.registered&&checks.prompt.set;
    if(!checks.runtime.ok||!toolsOk||!checks.desktop.ready||!checks.configuration.credentials||!accessConfigured())process.exitCode=1;return;
  }
  if(action!=='start')throw Error('支持命令：start、register-mcp、set-prompt、doctor、stop、backup、retry-delivery');
  try{if(!mcpRegistered(mcpToken(store)))log.warn('Codex 尚未注册或需更新 MCP 工具（浏览器、飞书 CLI），请运行 register-mcp.cmd 后重启 Codex Desktop');if(!promptCurrent())log.warn('Codex 系统提示词未设置或需更新（交付目录等约定写在里面），请运行 set-prompt.cmd');}catch(e){log.warn({error:safeError(e)},'无法读取 Codex 配置');}
  // Task state lives in SQLite and is reconciled on every pass, so one stray error should be logged rather than take the service down.
  process.on('unhandledRejection',e=>log.error({error:safeError(e)},'未处理的异步错误'));
  process.on('uncaughtException',e=>log.error({error:safeError(e)},'未捕获的异常'));
  let stopping=false;let poll:NodeJS.Timeout|undefined,refresh:NodeJS.Timeout|undefined,maintain:NodeJS.Timeout|undefined;
  let feishuStarted=false,lastFeishuAttempt=0,lastReconcile=0,lastNameSync=0,nameSync:Promise<void>|undefined;
  const token=randomBytes(32).toString('hex'),browsers=new BrowserManager(),mcp=new McpServer(store,browsers,new FeishuCliAuth());
  const feishu=new FeishuClient(),service=new TaskService(store,desktop,feishu),scheduler=new Scheduler(service),delivery=new Delivery(store,feishu);
  const server=http.createServer((request,response)=>{
    response.setHeader('Content-Type','application/json; charset=utf-8');response.setHeader('Cache-Control','no-store');
    if(request.headers.origin){response.writeHead(403).end('{}');return;}
    if(request.url==='/shutdown'&&request.method==='POST'){
      const given=Buffer.from(request.headers.authorization??''),expected=Buffer.from(`Bearer ${token}`);
      if(given.length!==expected.length||!timingSafeEqual(given,expected)){response.writeHead(403).end('{}');return;}
      response.end('{"stopping":true}');void stop();return;
    }
    if(request.url==='/mcp'){void mcp.handle(request,response).catch(()=>{if(!response.headersSent)response.writeHead(500).end('{}');});return;}
    if(request.method!=='GET'||!['/health','/ready'].includes(request.url??'')){response.writeHead(404).end('{}');return;}
    const ready=desktop.ready&&feishu.connected&&accessConfigured();
    response.writeHead(request.url==='/ready'&&!ready?503:200).end(JSON.stringify({ready,access:{mode:config.tenants.length?'tenant':'users',tenantCount:config.tenants.length},desktop:{ready:desktop.ready,version:desktop.version,error:desktop.error},feishu:{connected:feishu.connected,state:feishu.state,error:feishu.lastError},...store.stats()}));
  });
  const stop=async()=>{
    if(stopping)return;stopping=true;clearInterval(poll);clearInterval(refresh);clearInterval(maintain);server.close();
    const forced=setTimeout(()=>process.exit(0),15000);forced.unref();
    desktop.close();await browsers.close();await scheduler.stop();await delivery.stop();await nameSync;feishu.close();store.close();
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
      // The SDK gives up after a non-retryable error (for example rejected credentials); build a fresh client instead of staying deaf.
      if(feishuStarted&&feishu.state==='failed'){log.warn({error:feishu.lastError},'飞书长连接已失效，正在重建');feishu.close();feishuStarted=false;lastFeishuAttempt=0;}
      if(!feishuStarted&&Date.now()-lastFeishuAttempt>15000){lastFeishuAttempt=Date.now();
        try{await feishu.start(message=>service.receive(message));feishuStarted=true;log.info('飞书长连接已启动');}
        catch(e){feishu.close();feishu.lastError=safeError(e);log.warn({error:feishu.lastError},'等待飞书配置或连接');}}
      if(!nameSync&&desktop.ready&&feishuStarted&&Date.now()-lastNameSync>60000){
        lastNameSync=Date.now();nameSync=service.syncSessionNames().catch(e=>log.warn({error:safeError(e)},'会话命名检查失败')).finally(()=>{nameSync=undefined;});
      }
    }finally{polling=false;}
  };
  poll=setInterval(()=>{void reconnect().catch(e=>log.error({error:safeError(e)},'恢复检查失败'));},5000);
  refresh=setInterval(()=>{if(desktop.ready)for(const task of store.active())if(task.thread_id)void desktop.readTask(task.thread_id).catch(()=>{});},3000);
  maintain=setInterval(()=>{if(desktop.ready)void cleanup(store,desktop).catch(e=>log.warn({error:safeError(e)},'保留策略清理失败'));},3600000);
  log.info({root:config.root,health:`http://127.0.0.1:${config.HEALTH_PORT}/health`},'飞书 Desktop 机器人已启动');
  if(!accessConfigured())log.warn('未配置开放企业或用户；任务执行关闭，可通过 /whoami 获取租户和 Open ID');
  await reconnect();
}
main().catch(e=>{log.error({error:safeError(e)},'启动失败');process.exitCode=1;});
