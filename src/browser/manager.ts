import path from 'node:path';
import fs from 'node:fs/promises';
import {chromium,type BrowserContext,type Cookie,type Download,type Page} from 'playwright-core';
import {config} from '../config.js';
import {log,safeError} from '../log.js';

const ACTION_TIMEOUT=15000,NAVIGATION_TIMEOUT=30000,TEXT_LIMIT=6000,ELEMENT_LIMIT=150;
const SESSION_COOKIES='session-cookies.json',SESSION_COOKIE_DAYS=14;
const REF='data-lark-ref';
const DESCRIBE=`(limit)=>{
  document.querySelectorAll('[${REF}]').forEach(e=>e.removeAttribute('${REF}'));
  const selector='a[href],button,input,select,textarea,summary,[role=button],[role=link],[role=checkbox],[role=tab],[role=menuitem],[role=option],[role=switch],[contenteditable=""],[contenteditable=true],[onclick]';
  const lines=[];let n=0,skipped=0;
  for(const el of document.querySelectorAll(selector)){
    const box=el.getBoundingClientRect(),style=getComputedStyle(el);
    if(!box.width||!box.height||style.visibility==='hidden'||style.display==='none'||el.disabled&&el.type==='hidden')continue;
    if(n>=limit){skipped++;continue;}
    el.setAttribute('${REF}',String(++n));
    const tag=el.tagName.toLowerCase(),type=el.getAttribute('type')||el.getAttribute('role')||'';
    const label=(el.getAttribute('aria-label')||el.innerText||el.getAttribute('placeholder')||el.getAttribute('title')||el.getAttribute('alt')||'').replace(/\\s+/g,' ').trim().slice(0,80);
    let line='['+n+'] '+tag+(type?'('+type+')':'')+(label?' "'+label+'"':'');
    if(el.value&&type!=='password')line+=' value="'+String(el.value).slice(0,60)+'"';
    if(el.checked)line+=' checked';
    if(el.href)line+=' -> '+el.href.slice(0,120);
    lines.push(line);
  }
  if(skipped)lines.push('... '+skipped+' more elements not listed');
  return {lines,text:document.body?document.body.innerText:''};
}`;
const normalize=(text:string)=>text.replace(/\n{3,}/g,'\n\n').trim();

class UserBrowser {
  private pages:Page[]=[];private current?:Page;private chain:Promise<unknown>=Promise.resolve();private idle?:NodeJS.Timeout;
  private notes:string[]=[];
  private constructor(readonly root:string,private context:BrowserContext,private onClosed:()=>void){
    context.on('page',page=>{this.pages.push(page);this.current=page;page.on('close',()=>{this.pages=this.pages.filter(x=>x!==page);if(this.current===page)this.current=this.pages.at(-1);});
      page.on('download',download=>{void this.keepDownload(download);});});
    context.on('close',()=>{clearTimeout(this.idle);this.onClosed();});
    for(const page of context.pages()){this.pages.push(page);this.current=page;}
  }
  static async launch(root:string,onClosed:()=>void){
    // The profile (cookies, local storage, logins) lives with the user session, so users never share browser state.
    const context=await chromium.launchPersistentContext(path.join(root,'browser'),{
      headless:true,viewport:{width:1280,height:800},locale:'zh-CN',acceptDownloads:true,
      ...(config.BROWSER_EXECUTABLE?{executablePath:config.BROWSER_EXECUTABLE}:{channel:config.BROWSER_CHANNEL}),
    });
    context.setDefaultTimeout(ACTION_TIMEOUT);context.setDefaultNavigationTimeout(NAVIGATION_TIMEOUT);
    // Chromium drops session cookies (no Expires) when it exits, which would log users out on every idle close; restore them from disk.
    try{
      const saved=JSON.parse(await fs.readFile(path.join(root,'browser',SESSION_COOKIES),'utf8')) as Cookie[];
      await context.addCookies(saved.map(c=>({...c,expires:Date.now()/1000+SESSION_COOKIE_DAYS*86400})));
    }catch{}
    return new UserBrowser(root,context,onClosed);
  }
  private async keepDownload(download:Download){
    try{
      const directory=path.join(this.root,'downloads');await fs.mkdir(directory,{recursive:true});
      const target=path.join(directory,`${Date.now()}-${path.basename(download.suggestedFilename())}`);
      await download.saveAs(target);this.notes.push(`downloaded: ${target}`);
    }catch(e){this.notes.push(`download failed: ${safeError(e)}`);}
  }
  /** Commands for one user run strictly one at a time; the idle timer restarts after each. */
  run(command:string,args:string[],outputRoot?:string):Promise<string>{
    const next=this.chain.then(()=>this.execute(command,args,outputRoot),()=>this.execute(command,args,outputRoot)).finally(()=>this.saveSessionCookies());
    this.chain=next.catch(()=>{});
    return next.finally(()=>{clearTimeout(this.idle);this.idle=setTimeout(()=>{void this.close();},config.BROWSER_IDLE_MS);this.idle.unref();});
  }
  private async saveSessionCookies(){
    try{
      const session=(await this.context.cookies()).filter(c=>c.expires===-1);
      await fs.writeFile(path.join(this.root,'browser',SESSION_COOKIES),JSON.stringify(session),{mode:0o600});
    }catch{}
  }
  async close(){clearTimeout(this.idle);await this.saveSessionCookies();await this.context.close().catch(()=>{});}
  private async page():Promise<Page>{
    if(this.current&&!this.current.isClosed())return this.current;
    return this.context.newPage();
  }
  private target(page:Page,value:string|undefined){
    if(!value)throw Error('缺少元素：传入 snapshot 里的编号，或 Playwright 选择器');
    return page.locator(/^\d+$/.test(value)?`[${REF}="${value}"]`:value).first();
  }
  private async settle(page:Page){await page.waitForLoadState('domcontentloaded',{timeout:5000}).catch(()=>{});await page.waitForTimeout(300);}
  private async status(page:Page){
    const extra=this.notes.splice(0);
    return [`url: ${page.url()}`,`title: ${await page.title().catch(()=>'')}`,...extra].join('\n');
  }
  private async snapshot(page:Page){
    const {lines,text}=await page.evaluate(`(${DESCRIBE})(${ELEMENT_LIMIT})`) as {lines:string[];text:string};
    const body=normalize(text);
    return `${await this.status(page)}\n\nelements:\n${lines.join('\n')||'(none)'}\n\ntext:\n${body.slice(0,TEXT_LIMIT)}${body.length>TEXT_LIMIT?'\n... (truncated, use the text command for the full page)':''}`;
  }
  private checkOutput(file:string|undefined,outputRoot:string|undefined){
    if(!file||!path.isAbsolute(file)||!outputRoot)throw Error('截图路径必须是绝对路径，并位于交付目录下');
    const resolved=path.resolve(file),inside=path.relative(outputRoot,resolved);
    if(inside.startsWith('..')||path.isAbsolute(inside))throw Error('截图只能保存在当前任务的交付目录下');
    if(!/\.(png|jpe?g)$/i.test(resolved))throw Error('截图文件必须是 .png 或 .jpg');
    return resolved;
  }
  private async execute(command:string,args:string[],outputRoot?:string):Promise<string>{
    if(command==='close'){await this.close();return 'browser closed (profile and cookies are kept)';}
    if(command==='tabs')return (await Promise.all(this.pages.map(async(p,i)=>`${p===this.current?'*':' '} ${i} ${p.url()} ${await p.title().catch(()=>'')}`))).join('\n')||'(no tabs)';
    if(command==='tab'){
      const page=this.pages[Number(args[0])];if(!page)throw Error('没有这个标签页，先用 tabs 查看');
      this.current=page;await page.bringToFront();return this.status(page);
    }
    const page=await this.page();
    switch(command){
      case 'open':{
        const url=args[0];let parsed:URL;
        try{parsed=new URL(url);}catch{throw Error('请传入完整网址，例如 https://example.com');}
        if(!['http:','https:'].includes(parsed.protocol))throw Error('只允许 http 和 https 网址');
        await page.goto(parsed.href,{waitUntil:'domcontentloaded'});await this.settle(page);return this.snapshot(page);
      }
      case 'snapshot':return this.snapshot(page);
      case 'text':{const text=normalize(await page.evaluate('document.body?document.body.innerText:""') as string);return text.slice(0,60000)+(text.length>60000?'\n... (truncated)':'');}
      case 'click':await this.target(page,args[0]).click();await this.settle(page);return this.status(page);
      case 'fill':await this.target(page,args[0]).fill(args[1]??'');return 'ok';
      case 'select':await this.target(page,args[0]).selectOption(args[1]??'');return 'ok';
      case 'press':{if(!args[0])throw Error('缺少按键，例如 Enter');await page.keyboard.press(args[0]);await this.settle(page);return this.status(page);}
      case 'back':await page.goBack({waitUntil:'domcontentloaded'});await this.settle(page);return this.status(page);
      case 'wait':{
        const value=args[0]??'1000';
        if(/^\d+$/.test(value))await page.waitForTimeout(Math.min(Number(value),30000));else await page.locator(value).first().waitFor({timeout:30000});
        return this.status(page);
      }
      case 'eval':{
        if(!args[0])throw Error('缺少要执行的 JavaScript 表达式');
        const result=await page.evaluate(args[0]);const text=typeof result==='string'?result:JSON.stringify(result);
        return (text??'undefined').slice(0,TEXT_LIMIT*4);
      }
      case 'screenshot':{
        const file=this.checkOutput(args.find(a=>!a.startsWith('--')),outputRoot);
        await fs.mkdir(path.dirname(file),{recursive:true});await page.screenshot({path:file,fullPage:args.includes('--full')});return `saved: ${file}`;
      }
      default:throw Error(`未知命令 ${command}。支持：open snapshot text click fill select press back wait eval screenshot tabs tab close`);
    }
  }
}

/** One lazily launched headless browser per user session, keyed by the session directory. */
export class BrowserManager {
  private browsers=new Map<string,Promise<UserBrowser>>();private closed=false;
  async run(root:string,command:string,args:string[],outputRoot?:string):Promise<string>{
    if(this.closed)throw Error('服务正在停止');
    if(command==='close'&&!this.browsers.has(root))return 'browser was not running';
    let pending=this.browsers.get(root);
    if(!pending){
      pending=UserBrowser.launch(root,()=>{this.browsers.delete(root);});this.browsers.set(root,pending);
      pending.catch(e=>{this.browsers.delete(root);log.warn({error:safeError(e)},'浏览器启动失败');});
    }
    return (await pending).run(command,args,outputRoot);
  }
  async close(){this.closed=true;await Promise.all([...this.browsers.values()].map(p=>p.then(b=>b.close(),()=>{})));}
}
