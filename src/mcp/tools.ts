import path from 'node:path';
import type {BrowserManager} from '../browser/manager.js';
import type {FeishuCliAuth} from '../feishu-cli/auth.js';
import {runFeishuCli} from '../feishu-cli/run.js';
import {config} from '../config.js';

/** Who is calling, resolved by the server from Codex's own thread metadata, never from tool arguments. */
/** data: this user's own directory (browser profile, Feishu CLI login); workspace: this task's output directory. */
export interface ToolContext{data:string;workspace:string;browsers:BrowserManager;feishu:FeishuCliAuth}
export interface Tool{name:string;description:string;inputSchema:object;enabled:()=>boolean;run:(context:ToolContext,args:Record<string,unknown>)=>Promise<{text:string;isError?:boolean}>}

const text=(description:string)=>({type:'string',description});
const schema=(properties:Record<string,object>={},required:string[]=[])=>({type:'object',properties,required,additionalProperties:false});
const need=(args:Record<string,unknown>,key:string):string=>{const value=args[key];if(typeof value!=='string'||!value)throw Error(`缺少参数 ${key}`);return value;};
const LIMIT=100*1024;
const clip=(value:string)=>value.length>LIMIT?`${value.slice(0,LIMIT)}\n... (truncated)`:value;
const browserOn=()=>config.BROWSER_ENABLED;

const browser=(name:string,description:string,inputSchema:object,command:string,arguments_:(args:Record<string,unknown>)=>string[]=()=>[]):Tool=>({
  name,description,inputSchema,enabled:browserOn,
  run:async(context,args)=>({text:clip(await context.browsers.run(context.data,command,arguments_(args)))}),
});
const TARGET='Element ref number from the last browser_open/browser_snapshot, or a Playwright selector.';

export const tools:Tool[]=[
  browser('browser_open','Open an http(s) URL in this user\'s private headless browser (cookies and logins persist across tasks) and return the page elements and text. If the site needs a login or verification code, ask the user in the final reply.',
    schema({url:text('Full http or https URL')},['url']),'open',a=>[need(a,'url')]),
  browser('browser_snapshot','List the visible interactive elements as [ref] lines plus the page text. Run again after the page changes.',schema(),'snapshot'),
  browser('browser_text','Return the full text of the current page.',schema(),'text'),
  browser('browser_click','Click an element.',schema({target:text(TARGET)},['target']),'click',a=>[need(a,'target')]),
  browser('browser_fill','Fill a text field with a value.',schema({target:text(TARGET),text:text('Value to enter')},['target','text']),'fill',a=>[need(a,'target'),String(a.text??'')]),
  browser('browser_select','Choose an option in a select element.',schema({target:text(TARGET),value:text('Option value or label')},['target','value']),'select',a=>[need(a,'target'),need(a,'value')]),
  browser('browser_press','Press a keyboard key such as Enter or Tab.',schema({key:text('Key name')},['key']),'press',a=>[need(a,'key')]),
  browser('browser_back','Go back one page.',schema(),'back'),
  browser('browser_wait','Wait for a number of milliseconds (up to 30000) or for a selector to appear.',schema({for:text('Milliseconds, or a selector')},['for']),'wait',a=>[need(a,'for')]),
  browser('browser_eval','Evaluate a JavaScript expression in the current page and return the result.',schema({expression:text('JavaScript expression')},['expression']),'eval',a=>[need(a,'expression')]),
  browser('browser_tabs','List the open tabs.',schema(),'tabs'),
  browser('browser_tab','Switch to a tab by its index from browser_tabs.',schema({index:text('Tab index')},['index']),'tab',a=>[String(a.index??'')]),
  browser('browser_close','Close the browser; the profile and cookies are kept.',schema(),'close'),
  {name:'browser_screenshot',enabled:browserOn,
    description:'Save a screenshot of the current page into this task\'s delivery directory and return its absolute path; link that path in the final reply to deliver it.',
    inputSchema:schema({name:text('Optional file name, e.g. page.png'),full:{type:'boolean',description:'Capture the full page'}}),
    async run(context,args){
      const base=typeof args.name==='string'&&args.name?path.basename(args.name).replace(/[<>:"/\\|?*\x00-\x1f]/g,'_'):`screenshot-${Date.now()}.png`;
      const file=path.join(context.workspace,/\.(png|jpe?g)$/i.test(base)?base:`${base}.png`);
      return {text:await context.browsers.run(context.data,'screenshot',[file,...(args.full===true?['--full']:[])],context.workspace)};
    }},
  {name:'feishu_cli',enabled:()=>config.FEISHU_CLI_ENABLED&&!!config.FEISHU_APP_ID,
    description:'Run the official Feishu/Lark CLI (lark-cli) as this user, with this user\'s own login, for docs, drive, messages, calendar, tasks, sheets, base, wiki, contacts and more. Pass the arguments after "lark-cli", e.g. ["docs","--help"], ["schema","im.messages.create"] or ["api","GET","/open-apis/..."]. If it reports the user is not logged in, call it with ["auth","login"] and put the printed link in your final reply; the user authorizes and sends the request again. Also supported: ["auth","status"] and ["auth","logout"]. Prefer +shortcuts; read --help first.',
    inputSchema:schema({args:{type:'array',items:{type:'string'},description:'lark-cli arguments'}},['args']),
    async run(context,args){
      if(!Array.isArray(args.args)||!args.args.length||args.args.some(x=>typeof x!=='string'))throw Error('args 必须是非空字符串数组');
      const result=await runFeishuCli(context.feishu,context.data,args.args as string[],context.workspace);
      return {text:clip([result.stdout,result.stderr].filter(Boolean).join('\n')||`(exit ${result.code})`),isError:result.code!==0};
    }},
];
