// One-time loopback configuration form. Credentials stay between the browser and this local process.
import http from 'node:http';
import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {randomBytes} from 'node:crypto';
import {execFileSync} from 'node:child_process';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const nonce=randomBytes(24).toString('hex'),route=`/setup/${nonce}`,origin='http://127.0.0.1:8766';
const server=http.createServer(async(req,res)=>{
  res.setHeader('Cache-Control','no-store');res.setHeader('Content-Type','text/html; charset=utf-8');
  res.setHeader('Content-Security-Policy',"default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'");
  if(req.url!==route){res.writeHead(404).end('Not found');return;}
  if(req.method==='GET'){res.end(`<!doctype html><html lang="zh"><meta charset="utf-8"><title>飞书机器人本地配置</title><style>body{font:16px system-ui;max-width:660px;margin:60px auto}label,input,textarea{display:block;width:100%;margin:12px 0}input,textarea,button{font:inherit;padding:10px;box-sizing:border-box}button{background:#1769ff;color:white;border:0;border-radius:6px}</style><h1>飞书机器人本地配置</h1><p>配置仅写入本项目 .env，不会加入交付包。无需安装系统 Node.js。</p><form method="post"><label>App ID<input name="appId" required autocomplete="off"></label><label>App Secret<input type="password" name="appSecret" required autocomplete="off"></label><label>允许执行任务的 Open ID（逗号分隔，可稍后配置）<textarea name="users"></textarea></label><button type="submit">保存到项目</button></form></html>`);return;}
  if(req.method!=='POST'||req.headers.origin!==origin){res.writeHead(403).end('Forbidden');return;}
  try{
    let body='';for await(const chunk of req){body+=chunk;if(body.length>16384)throw Error('配置过长');}
    const values=new URLSearchParams(body),id=values.get('appId')??'',secret=values.get('appSecret')??'',users=values.get('users')??'';
    if(!/^cli_[a-zA-Z0-9]+$/.test(id)||!secret||/[\r\n]/.test(secret+users))throw Error('配置格式错误');
    let text=await fs.readFile(path.join(root,'.env'),'utf8').catch(()=>fs.readFile(path.join(root,'.env.example'),'utf8'));
    for(const [key,value]of Object.entries({FEISHU_APP_ID:id,FEISHU_APP_SECRET:secret,ALLOWED_USERS:users})){
      const line=`${key}=${JSON.stringify(value)}`;text=new RegExp(`^${key}=.*$`,'m').test(text)?text.replace(new RegExp(`^${key}=.*$`,'m'),()=>line):text+'\n'+line;
    }
    const file=path.join(root,'.env');await fs.writeFile(file,text,{mode:0o600});
    if(process.platform==='win32'){const account=execFileSync('whoami.exe',[],{encoding:'utf8',windowsHide:true}).trim();execFileSync('icacls.exe',[file,'/inheritance:r','/grant:r',`${account}:(F)`,'*S-1-5-18:(F)'],{windowsHide:true,stdio:'pipe'});}
    res.end('<h1>已保存到本项目 .env</h1><p>凭证未回显。可以关闭此页面，继续启动机器人。</p>');server.close();
  }catch{res.writeHead(400).end('保存失败，请检查输入格式。');}
});
server.listen(8766,'127.0.0.1',()=>console.log(origin+route));
setTimeout(()=>server.close(),15*60*1000).unref();
