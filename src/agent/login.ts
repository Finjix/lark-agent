import readline from 'node:readline/promises';
import {spawn} from 'node:child_process';
import type {AuthInteraction} from '@earendil-works/pi-ai';
import {authFile,restrictSecret} from '../config.js';
import type {AgentRunner} from './runner.js';

/** Opens a link in the default browser of this machine; a failure is fine, the link is printed too. */
function open(url:string){
  const [command,args]=process.platform==='win32'?['cmd.exe',['/c','start','""',url.replaceAll('&','^&')]]:process.platform==='darwin'?['open',[url]]:['xdg-open',[url]];
  try{spawn(command,args,{stdio:'ignore',detached:true,windowsHide:true}).on('error',()=>{}).unref();}catch{}
}

/**
 * `start.cmd login [device]`: signs the bot in with a ChatGPT subscription (Plus/Pro) through pi's OAuth flow, in this console. The default
 * opens the browser of this machine; `device` shows a code to enter on any device instead (for a machine nobody sits at). The login is kept
 * in <project>/.pi/auth.json and its token is refreshed by pi as it is used; every Feishu user shares it.
 */
export async function login(runner:AgentRunner,method:'browser'|'device'='browser'){
  const console_=readline.createInterface({input:process.stdin,output:process.stdout});
  const interaction:AuthInteraction={
    prompt:async prompt=>{
      if(prompt.type==='select'){
        const wanted=method==='device'?/device/i:/browser/i;
        return (prompt.options.find(o=>wanted.test(o.id)||wanted.test(o.label))??prompt.options[0]!).id;
      }
      return (await console_.question(`${prompt.message}${prompt.placeholder?`（${prompt.placeholder}）`:''}\n> `,prompt.signal?{signal:prompt.signal}:{})).trim();
    },
    notify:event=>{
      if(event.type==='auth_url'){console.log(`\n在浏览器里登录 ChatGPT：\n${event.url}\n${event.instructions??''}`);open(event.url);}
      else if(event.type==='device_code')console.log(`\n在任意设备上打开 ${event.verificationUri}\n输入代码：${event.userCode}\n`);
      else console.log(event.message);
    },
  };
  try{
    await (await runner.models()).login(runner.provider,'oauth',interaction);
    restrictSecret(authFile());
    console.log(`已登录（${runner.provider}），机器人下次检查时（10 秒内）就绪，无需重启`);
  }finally{console_.close();}
}
export async function logout(runner:AgentRunner){
  await (await runner.models()).logout(runner.provider);
  console.log(`已退出登录（${runner.provider}）`);
}
