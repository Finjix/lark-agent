import http from 'node:http';
import fs from 'node:fs/promises';
import path from 'node:path';
import {randomBytes,timingSafeEqual} from 'node:crypto';
import {getShellConfig} from '@earendil-works/pi-coding-agent';
import {config,ensureDirectories,restrictSecret} from './config.js';
import {log,safeError} from './log.js';
import {Store} from './storage/database.js';
import {AgentRunner} from './agent/runner.js';
import {login,logout} from './agent/login.js';
import {ensurePython,pythonStatus} from './service/python.js';
import {Jobs,declaredJobs} from './service/jobs.js';
import {FeishuClient} from './feishu/client.js';
import {TaskService} from './tasks/service.js';
import {Scheduler} from './tasks/scheduler.js';
import {Delivery} from './feishu/delivery.js';
import {housekeeping,skillPrunes} from './service/maintenance.js';
import {accessConfigured} from './users/access.js';
import {clockOffset} from './feishu/clock.js';

/**
 * The routine check (the Feishu long connection, the skills' jobs, the clock, the ChatGPT login, the line in the log that the service is
 * alive): at the start, then hourly.
 */
const ROUTINE_MS=60*60000;
/**
 * A long connection the routine check finds not connected is looked at again this often until it is; this many failed looks in a row and it
 * is built afresh, whatever the SDK's own retries are doing.
 */
const FEISHU_RETRY_MS=30000,FEISHU_FAILURES=3,AGENT_RETRY_MS=10000;
/** Housekeeping (see maintenance.ts): at the start, then every 72 hours. */
const HOUSEKEEPING_MS=72*3600000;
const COMMANDS='start、setup、login [device]、logout、doctor、stop、backup、retry-delivery';

async function doctor(agent:AgentRunner){
  const store=new Store(config.database);
  const shell=(()=>{try{return {ok:true,path:getShellConfig().shell};}catch(e){return {ok:false,error:safeError(e)};}})();
  await agent.probe();
  const skills=agent.loadSkills(),{jobs,invalid}=await declaredJobs(),prunes=await skillPrunes();
  const checks:any={
    runtime:{ok:Number(process.versions.node.split('.')[0])>=24,version:process.version,executable:process.execPath},
    sqlite:{ok:true},configuration:{credentials:!!(config.FEISHU_APP_ID&&config.FEISHU_APP_SECRET),allowedUsers:config.users.length,administrators:config.admins.length,allowedTenants:config.tenants.length},
    agent:{ready:agent.ready,pi:agent.version,model:`${agent.provider}/${agent.modelId}`,thinking:agent.thinking,home:config.agentHome,...(agent.error?{error:agent.error}:{})},
    shell,python:await pythonStatus(),
    // The agent's own Feishu tools (the user's login, documents) and the bridge the skills reach Feishu through.
    feishuModule:{ok:await fs.stat(config.feishu).then(s=>s.isFile(),()=>false),path:path.relative(config.root,config.feishu)},
    skills:skills.map(s=>({name:s.name,path:path.relative(config.root,s.baseDir),...(prunes.some(p=>p.skill===s.name)?{prune:true}:{})})),
    jobs:[...jobs.map(j=>({name:j.name,everyMinutes:j.everyMinutes})),...invalid.map(reason=>({invalid:reason}))],
  };
  if(checks.configuration.credentials){try{const info=await new FeishuClient().raw('GET','/open-apis/bot/v3/info');checks.feishu={ok:true,botName:info.bot?.app_name??info.bot?.name};}catch(e){checks.feishu={ok:false,error:safeError(e)};}}
  store.close();console.log(JSON.stringify(checks,null,2));
  if(!checks.runtime.ok||!agent.ready||!shell.ok||!checks.feishuModule.ok||!checks.python.ok||!checks.python.installed||!checks.configuration.credentials||!accessConfigured()||invalid.length)process.exitCode=1;
}

async function main(){
  ensureDirectories();const action=process.argv[2]??'start';
  if(action==='stop'){
    const token=await fs.readFile(path.join(config.data,'control.token'),'utf8');
    const response=await fetch(`http://127.0.0.1:${config.HEALTH_PORT}/shutdown`,{method:'POST',headers:{authorization:`Bearer ${token}`}});
    if(!response.ok)throw Error(`停止请求失败：${response.status}`);console.log('已请求安全停止');return;
  }
  // The skills' Python packages are made ready before the service starts (`start`), or on their own (`setup`).
  if(action==='setup'){await ensurePython();console.log(`技能的 Python 依赖已就绪：${config.python}`);return;}
  if(action==='start')await ensurePython();
  const agent=new AgentRunner();
  if(action==='login'){await login(agent,process.argv[3]==='device'?'device':'browser');agent.close();return;}
  if(action==='logout'){await logout(agent);agent.close();return;}
  if(action==='doctor'){await doctor(agent);agent.close();return;}
  const store=new Store(config.database);
  if(action==='retry-delivery'){
    const id=process.argv[3];if(!id)throw Error('请指定任务 ID 或明确传入 all');
    const sql="UPDATE delivery_outbox SET status='PENDING',next_attempt_at=0,version=version+1 WHERE status IN ('FAILED','RETRYING')";
    const result=id==='all'?store.db.prepare(sql).run():store.db.prepare(sql+' AND task_id=?').run(id);
    store.close();console.log(`已恢复 ${result.changes} 条投递，不会重新执行任务`);return;
  }
  if(action==='backup'){
    const file=path.join(config.data,`backup-${Date.now()}.sqlite`);await store.db.backupTo(file);store.close();console.log(file);return;
  }
  if(action!=='start'){store.close();throw Error(`支持命令：${COMMANDS}`);}
  // Task state lives in SQLite and is reconciled on every pass, so one stray error should be logged rather than take the service down.
  process.on('unhandledRejection',e=>log.error({error:safeError(e)},'未处理的异步错误'));
  process.on('uncaughtException',e=>log.error({error:safeError(e)},'未捕获的异常'));
  let stopping=false;let agentRetry:NodeJS.Timeout|undefined,poll:NodeJS.Timeout|undefined,feishuRetry:NodeJS.Timeout|undefined,maintain:NodeJS.Timeout|undefined;
  // Housekeeping is told to stop and awaited on shutdown, so it never meets a closed database.
  const halt=new AbortController();let sweeping:Promise<void>=Promise.resolve();
  const housekeep=()=>{sweeping=housekeeping(store,halt.signal);};
  let feishuStarted=false,lastAgentError='';
  // For /health: when the last round of checks finished. A round that never finishes means something in it hangs.
  let lastPoll=0,feishuState='idle',feishuDownSince=0,started=false;
  // Until the service has started, what it waits for is shown on the console (each reason once), so a quiet window is not taken for a started one.
  const shown=new Set<string>(),waiting=(text:string)=>{if(!started&&!shown.has(text)){shown.add(text);log.console(`等待：${text}`,'warn');}};
  const token=randomBytes(32).toString('hex'),feishu=new FeishuClient();
  const service=new TaskService(store,agent,feishu),scheduler=new Scheduler(service),delivery=new Delivery(store,feishu),jobs=new Jobs();
  const server=http.createServer((request,response)=>{
    response.setHeader('Content-Type','application/json; charset=utf-8');response.setHeader('Cache-Control','no-store');
    if(request.headers.origin){response.writeHead(403).end('{}');return;}
    if(request.url==='/shutdown'&&request.method==='POST'){
      const given=Buffer.from(request.headers.authorization??''),expected=Buffer.from(`Bearer ${token}`);
      if(given.length!==expected.length||!timingSafeEqual(given,expected)){response.writeHead(403).end('{}');return;}
      response.end('{"stopping":true}');void stop();return;
    }
    if(request.method!=='GET'||!['/health','/ready'].includes(request.url??'')){response.writeHead(404).end('{}');return;}
    const ready=agent.ready&&feishu.connected&&accessConfigured();
    response.writeHead(request.url==='/ready'&&!ready?503:200).end(JSON.stringify({ready,access:{mode:config.tenants.length?'tenant':'users',tenantCount:config.tenants.length},
      uptimeMs:Math.round(process.uptime()*1000),pollAgeMs:lastPoll?Date.now()-lastPoll:null,
      agent:{ready:agent.ready,pi:agent.version,model:`${agent.provider}/${agent.modelId}`,error:agent.error},skills:agent.skills.map(s=>s.name),jobs:jobs.status(),
      feishu:{connected:feishu.connected,state:feishu.state,reconnectAttempts:feishu.reconnectAttempts,error:feishu.lastError},...store.stats()}));
  });
  const stop=async()=>{
    if(stopping)return;stopping=true;clearInterval(poll);clearInterval(agentRetry);clearTimeout(feishuRetry);clearInterval(maintain);halt.abort();server.close();
    const forced=setTimeout(()=>process.exit(0),15000);forced.unref();
    // The agent runs in this service and ends with it: the tasks are finished as failed, and one last delivery pass finishes their cards.
    // Nothing new starts once stopping: the scheduler first, then what runs is stopped.
    await scheduler.stop();service.stop();await jobs.stop();await delivery.flush().catch(()=>{});await delivery.stop();await sweeping;
    feishu.close();agent.close();store.close();
    await fs.rm(path.join(config.data,'control.token'),{force:true});log.info('服务已安全停止');clearTimeout(forced);
    // The last lines still go to the console; one that has stalled must not keep the process from ending.
    setTimeout(()=>process.exit(),5000).unref();
  };
  try{await new Promise<void>((resolve,reject)=>{server.once('error',reject);server.listen(config.HEALTH_PORT,'127.0.0.1',resolve);});}
  catch(e){
    store.close();
    if((e as NodeJS.ErrnoException).code==='EADDRINUSE')throw Error(`端口 ${config.HEALTH_PORT} 已被占用，可能已有一份服务在运行`);
    throw e;
  }
  await fs.writeFile(path.join(config.data,'control.token'),token,{mode:0o600});
  restrictSecret(path.join(config.data,'control.token'));
  process.once('SIGINT',()=>{void stop();});process.once('SIGTERM',()=>{void stop();});
  // Before anything can run or send: what the previous run left unfinished is over and must not reach users now.
  const leftovers=store.discardLeftovers();if(leftovers.cancelled||leftovers.dropped||leftovers.interrupted)log.info(leftovers,'已作废上次运行遗留的任务和未送达消息');
  // The first round runs before anything can start: no task is running yet, so nothing is skipped.
  housekeep();await sweeping;
  const skills=agent.loadSkills();log.info({skills:skills.map(s=>s.name)},skills.length?'技能已就绪':'skills/ 里没有技能');
  // Skills write their jobs while they are used (data/shared/<skill>/jobs.json), so the files are read again at every routine check.
  let invalidJobs='';
  const readJobs=async()=>{
    const declared=await declaredJobs();
    const reasons=declared.invalid.join('\n');if(reasons!==invalidJobs){invalidJobs=reasons;for(const reason of declared.invalid)log.warn({reason},'定时任务无效，已跳过');}
    jobs.update(declared.jobs);
  };
  await readJobs();
  scheduler.start();delivery.start();
  const announceStarted=()=>{
    if(started||!agent.ready||!feishu.connected)return;
    started=true;const names=agent.skills.map(s=>s.name);
    log.console(`启动成功，技能：${names.length?names.join('、'):'无'}${accessConfigured()?'':'（未配置开放的企业或用户，任务不会执行）'}`);
  };
  /** Whether the agent can run (the ChatGPT login, the model): at every routine check, and every AGENT_RETRY_MS while it cannot. */
  const checkAgent=async()=>{
    const was=agent.ready;await agent.probe();
    if(agent.ready&&!was){log.info({pi:agent.version,model:`${agent.provider}/${agent.modelId}`},'智能体已就绪');lastAgentError='';announceStarted();}
    else if(!agent.ready&&agent.error!==lastAgentError){lastAgentError=agent.error;log.warn({error:agent.error},'等待智能体就绪');}
    if(!agent.ready&&agent.error)waiting(agent.error);
  };
  /** The routine check, every ROUTINE_MS: the long connection, the skills' jobs, the clock, the ChatGPT login, and a line in the log that the service is alive. */
  let checking=false;
  const routine=async()=>{
    if(checking||stopping)return;checking=true;
    try{
      // The agent first: messages the connection brings in are refused while it is not ready.
      await checkAgent();
      await connection().catch(e=>log.error({error:safeError(e)},'飞书长连接检查失败'));
      await readJobs().catch(e=>log.warn({error:safeError(e)},'读取定时任务失败'));
      void clockOffset().then(offset=>{service.clockOffsetMs=offset;if(Math.abs(offset)>5000)log.warn({offsetMs:Math.round(offset)},'本机时钟与飞书相差较大，已按飞书时间判断消息是否过期，请校准系统时间');}).catch(e=>log.debug({error:safeError(e)},'读取飞书时间失败'));
      log.info({feishu:feishu.state,agent:agent.ready,running:service.running,rssMB:Math.round(process.memoryUsage().rss/1048576)},'服务运行中');
      announceStarted();
    }finally{checking=false;lastPoll=Date.now();}
  };
  /**
   * The long connection, at each routine check: not started yet (or rebuilt), it is started; started but not connected, that is a failed
   * look, and after FEISHU_FAILURES of them in a row (or at once, when the SDK has given up: rejected credentials, say) the client is closed
   * and built afresh. While it is not connected it is looked at again every FEISHU_RETRY_MS, until it is.
   */
  let connecting=false,feishuFailures=0;
  const connection=async()=>{
    if(connecting||stopping)return;connecting=true;
    try{
      const state=feishuStarted?feishu.state:'idle';
      if(feishuStarted&&state!==feishuState){
        if(state==='connected')log.info({downMs:feishuDownSince?Date.now()-feishuDownSince:undefined},'飞书长连接已连通');
        else log.warn({state,reconnectAttempts:feishu.reconnectAttempts,error:feishu.lastError||undefined},'飞书长连接已断开');
      }
      feishuState=state;
      if(state==='connected'){feishuDownSince=0;feishuFailures=0;}
      else if(feishuStarted){
        if(!feishuDownSince)feishuDownSince=Date.now();
        feishuFailures++;
        if(state==='failed'||feishuFailures>=FEISHU_FAILURES){
          log.warn({state,failures:feishuFailures,downMs:Date.now()-feishuDownSince,error:feishu.lastError},'飞书长连接已失效，正在重建');
          feishu.close();feishuStarted=false;feishuState='idle';feishuFailures=0;
        }
      }
      if(!feishuStarted){
        try{await feishu.start(message=>service.receive(message));feishuStarted=true;feishuDownSince=feishu.connected?0:Date.now();log.info('飞书长连接已启动');}
        catch(e){feishu.close();feishu.lastError=safeError(e);log.warn({error:feishu.lastError},'等待飞书配置或连接');waiting(`飞书长连接：${feishu.lastError}`);}
      }
      announceStarted();
    }finally{
      connecting=false;
      if(feishu.connected||stopping){clearTimeout(feishuRetry);feishuRetry=undefined;}
      else if(!feishuRetry)feishuRetry=setTimeout(()=>{feishuRetry=undefined;void connection().catch(e=>log.error({error:safeError(e)},'飞书长连接检查失败'));},FEISHU_RETRY_MS);
    }
  };
  poll=setInterval(()=>{void routine().catch(e=>log.error({error:safeError(e)},'例行检查失败'));},ROUTINE_MS);
  maintain=setInterval(housekeep,HOUSEKEEPING_MS);
  // A login made while the service runs (start.cmd login), or a passing failure of the check, is picked up within seconds, not at the next hour.
  agentRetry=setInterval(()=>{if(!agent.ready&&!stopping)void checkAgent().catch(e=>log.debug({error:safeError(e)},'检查智能体失败'));},AGENT_RETRY_MS);agentRetry.unref();
  log.info({root:config.root,health:`http://127.0.0.1:${config.HEALTH_PORT}/health`},'飞书机器人已启动');
  if(!accessConfigured())log.warn('未配置开放企业或用户；任务执行关闭；未获授权的人发消息时，机器人会回复他的 Open ID 和租户');
  await routine();
}
main().catch(e=>{log.console(`启动失败：${safeError(e)}`,'error');process.exitCode=1;});
