import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {config} from '../dist/config.js';
import {FeishuCliAuth,loadCredentials} from '../dist/feishu-cli/auth.js';
import {runFeishuCli} from '../dist/feishu-cli/run.js';

const originalFetch=globalThis.fetch;
const json=(body,status=200)=>new Response(JSON.stringify(body),{status,headers:{'content-type':'application/json'}});
async function withApp(fn){
  const saved={id:config.FEISHU_APP_ID,secret:config.FEISHU_APP_SECRET,path:config.FEISHU_CLI_PATH,scopes:config.FEISHU_CLI_SCOPES};
  config.FEISHU_APP_ID='cli_test';config.FEISHU_APP_SECRET='app-secret';config.FEISHU_CLI_SCOPES=['docx:document'];
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-fcli-'));
  try{await fn(root);}finally{
    globalThis.fetch=originalFetch;config.FEISHU_APP_ID=saved.id;config.FEISHU_APP_SECRET=saved.secret;config.FEISHU_CLI_PATH=saved.path;config.FEISHU_CLI_SCOPES=saved.scopes;
    await fs.rm(root,{recursive:true,force:true});
  }
}

test('device login stores the user token inside the session directory',()=>withApp(async root=>{
  const calls=[];let polls=0;
  globalThis.fetch=async(url,init)=>{
    const body=Object.fromEntries(new URLSearchParams(String(init.body)));calls.push({url:String(url),body,auth:init.headers.authorization});
    if(String(url).includes('device_authorization'))return json({device_code:'dc',user_code:'ABCD-1234',verification_uri:'https://accounts.feishu.cn/x',verification_uri_complete:'https://accounts.feishu.cn/x?code=ABCD',expires_in:60,interval:1});
    return ++polls===1?json({error:'authorization_pending'},400):json({access_token:'u-token',refresh_token:'r-token',expires_in:7200,refresh_token_expires_in:604800,scope:'docx:document offline_access'});
  };
  const auth=new FeishuCliAuth(),text=await auth.login(root);
  assert(text.includes('https://accounts.feishu.cn/x?code=ABCD')&&text.includes('ABCD-1234'));
  assert.equal(calls[0].body.scope,'offline_access docx:document');assert(calls[0].auth.startsWith('Basic '));
  assert.equal(await loadCredentials(root),undefined,'nothing is stored before the user authorizes');
  assert.equal(await auth.login(root),text.replace(/\d+ 分钟/,m=>m),'a second login while pending reuses the same authorization');
  for(let i=0;i<60&&!(await loadCredentials(root));i++)await new Promise(r=>setTimeout(r,100));
  const stored=await loadCredentials(root);assert.equal(stored.accessToken,'u-token');assert.equal(stored.refreshToken,'r-token');
  assert((await fs.stat(path.join(root,'feishu-cli','credentials.json'))).isFile());
  assert((await auth.status(root)).startsWith('已登录'));
  assert.equal(await auth.token(root),'u-token');
  await auth.logout(root);assert.equal(await loadCredentials(root),undefined);assert.equal(await auth.token(root),undefined);
}));

test('an expiring token is refreshed once and the rotated refresh token is saved; a rejected one logs the user out',()=>withApp(async root=>{
  await fs.mkdir(path.join(root,'feishu-cli'),{recursive:true});
  const write=(c)=>fs.writeFile(path.join(root,'feishu-cli','credentials.json'),JSON.stringify(c));
  await write({accessToken:'old',expiresAt:Date.now()+1000,refreshToken:'r1',refreshExpiresAt:Date.now()+86400000,savedAt:0});
  let refreshes=0;
  globalThis.fetch=async(url,init)=>{refreshes++;const body=Object.fromEntries(new URLSearchParams(String(init.body)));assert.equal(body.grant_type,'refresh_token');assert.equal(body.refresh_token,'r1');
    await new Promise(r=>setTimeout(r,50));return json({access_token:'new',refresh_token:'r2',expires_in:7200,refresh_token_expires_in:604800});};
  const auth=new FeishuCliAuth();
  assert.deepEqual(await Promise.all([auth.token(root),auth.token(root)]),['new','new']);assert.equal(refreshes,1,'concurrent callers share one refresh');
  assert.equal((await loadCredentials(root)).refreshToken,'r2');
  await write({accessToken:'old',expiresAt:Date.now()+1000,refreshToken:'r1',refreshExpiresAt:Date.now()+86400000,savedAt:0});
  globalThis.fetch=async()=>json({error:'invalid_grant',error_description:'revoked'},400);
  assert.equal(await auth.token(root),undefined);assert.equal(await loadCredentials(root),undefined);
  await write({accessToken:'old',expiresAt:Date.now()+1000,refreshToken:'r1',refreshExpiresAt:Date.now()+86400000,savedAt:0});
  globalThis.fetch=async()=>{throw new Error('network down');};
  await assert.rejects(auth.token(root),/network down/);assert((await loadCredentials(root)).refreshToken==='r1','a network failure keeps the login');
}));

test('the CLI runs with the user token in its environment, user-only identity, and state confined to the session',()=>withApp(async root=>{
  const script=path.join(root,'probe.mjs');
  await fs.writeFile(script,"console.log(JSON.stringify({cwd:process.cwd(),args:process.argv.slice(2),env:Object.fromEntries(Object.entries(process.env).filter(([k])=>/^(LARKSUITE|SECRET_|FEISHU_|USERPROFILE)/.test(k)))}));");
  config.FEISHU_CLI_PATH=process.execPath;process.env.SECRET_SERVICE_VALUE='must-not-leak';
  const auth=new FeishuCliAuth();
  const missing=await runFeishuCli(auth,root,[script]);assert.equal(missing.code,1);assert(missing.stderr.includes('auth login'));
  await fs.mkdir(path.join(root,'feishu-cli'),{recursive:true});
  await fs.writeFile(path.join(root,'feishu-cli','credentials.json'),JSON.stringify({accessToken:'u-live',expiresAt:Date.now()+3600000,savedAt:0}));
  const inside=path.join(root,'output','t1');
  const run=async(workdir,...args)=>JSON.parse((await runFeishuCli(auth,root,[script,...args],workdir)).stdout);
  const result=await run(inside,'docs','--help');
  assert.equal(result.cwd,inside);assert.deepEqual(result.args,['docs','--help']);
  assert.equal(result.env.LARKSUITE_CLI_USER_ACCESS_TOKEN,'u-live');assert.equal(result.env.LARKSUITE_CLI_STRICT_MODE,'user');assert.equal(result.env.LARKSUITE_CLI_APP_ID,'cli_test');
  assert(result.env.LARKSUITE_CLI_CONFIG_DIR.startsWith(path.join(root,'feishu-cli')));assert(result.env.USERPROFILE.startsWith(path.join(root,'feishu-cli')));
  assert.equal(result.env.SECRET_SERVICE_VALUE,undefined,'the service environment is not passed on');
  assert.equal((await run(undefined,'x')).cwd,root,'without a task directory the user data directory is used');
  for(const bad of [['config','show'],['--profile','x','docs'],['auth','token']]){assert.equal((await runFeishuCli(auth,root,bad)).code,1);}
  delete process.env.SECRET_SERVICE_VALUE;
}));

