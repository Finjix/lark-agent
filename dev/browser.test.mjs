import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import {BrowserManager} from '../dist/browser/manager.js';

test('each user session gets its own headless profile: cookies never cross over',async t=>{
  const server=http.createServer((req,res)=>{
    if(req.url==='/login'){res.setHeader('Set-Cookie','sid=secret-of-a; Path=/');res.end('<p>logged in</p>');return;}
    res.setHeader('Content-Type','text/html');res.end(`<h1>cookie:[${req.headers.cookie??''}]</h1><a href="/login">go</a><input placeholder="q">`);
  });
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  const base=`http://127.0.0.1:${server.address().port}`,root=await fs.mkdtemp(path.join(os.tmpdir(),'lark-browser-'));
  const [a,b]=[path.join(root,'a'),path.join(root,'b')],manager=new BrowserManager();
  t.after(async()=>{await manager.close();server.close();await fs.rm(root,{recursive:true,force:true});});
  try{await manager.run(a,'open',[`${base}/`]);}
  catch(e){t.skip(`no usable browser on this machine: ${e.message.split('\n')[0]}`);return;}
  await manager.run(a,'open',[`${base}/login`]);
  assert((await manager.run(a,'open',[`${base}/`])).includes('cookie:[sid=secret-of-a]'));
  assert((await manager.run(b,'open',[`${base}/`])).includes('cookie:[]'),'user b must not see user a cookies');
  assert((await fs.stat(path.join(a,'browser'))).isDirectory()&&(await fs.stat(path.join(b,'browser'))).isDirectory());
  const snap=await manager.run(a,'snapshot',[]);assert(/\[1\] a/.test(snap)&&/\[2\] input/.test(snap));
  await manager.run(a,'fill',['2','hello']);assert((await manager.run(a,'snapshot',[])).includes('value="hello"'));
  const shot=path.join(a,'tasks','x','shot.png');assert.equal(await manager.run(a,'screenshot',[shot]),`saved: ${shot}`);assert((await fs.stat(shot)).size>0);
  await assert.rejects(manager.run(a,'screenshot',[path.join(b,'x.png')]),/交付目录/);
  await assert.rejects(manager.run(a,'screenshot',[path.join(a,'browser','x.png')]),/交付目录/);
  await assert.rejects(manager.run(a,'open',['file:///C:/Windows/win.ini']),/http/);
  // Cookies survive a browser restart because the profile is on disk.
  await manager.run(a,'close',[]);
  assert((await manager.run(a,'open',[`${base}/`])).includes('cookie:[sid=secret-of-a]'));
});
