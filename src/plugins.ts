import {createHash} from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';
import type {CodexOptions} from '@openai/codex-sdk';
import {config} from './config.js';
import {log,safeError} from './log.js';

/**
 * Plugins are dropped into <project>/plugins/ as folders (the folder holds plugin.json, skills/, mcp.json ...). At start the service installs
 * them into the project's Codex home, replaces the ones that changed and removes the ones that are gone; nothing else has to be configured.
 * Codex knows them through one marketplace, which is plugins/ itself with a generated plugins/.agents/plugins/marketplace.json.
 * A folder whose name starts with "." or "_" is ignored, which is how a plugin is switched off without deleting it.
 */
export const MARKETPLACE='lark-agent';
/**
 * What a plugin's MCP servers receive from the host with every user's turn: the user's folder, the user's Open ID and the bot's Feishu app.
 * Codex does not hand its own environment to the MCP servers of a plugin, so the host registers them again with these (names only: the
 * values never appear on a command line).
 */
export const PLUGIN_ENV=['LARK_AGENT_USER_DIR','LARK_AGENT_USER_OPEN_ID','FEISHU_APP_ID','FEISHU_APP_SECRET'] as const;
/** Whose turn it is, as far as plugins are concerned: `dir` is user/<name> (not its workspace), `openId` the Feishu Open ID. */
export interface PluginUser {dir:string;openId:string}

export interface McpServer {command:string;args:string[];cwd?:string;env:Record<string,string>}
/** An installed plugin. `path` is its copy inside the Codex home, `mcp` the stdio servers its mcp.json declares. */
export interface Plugin {id:string;name:string;version:string;path:string;mcp:Record<string,McpServer>}
/** A folder under plugins/ that holds a plugin. */
export interface Dropped {name:string;version:string;folder:string;hash:string}
export interface Skipped {folder:string;reason:string}
/** Runs the Codex CLI of this project to completion and returns what it printed. */
export type Cli=(args:string[])=>Promise<string>;

const NAME=/^[A-Za-z0-9][A-Za-z0-9_-]*$/;
const readJson=async(file:string)=>{const text=await fs.readFile(file,'utf8');return JSON.parse(text.charCodeAt(0)===0xFEFF?text.slice(1):text);};
const exists=(file:string)=>fs.stat(file).then(()=>true,()=>false);
/** Whether two paths are the same place; Codex writes a local marketplace's root with a \\?\ prefix. */
const same=(a:string,b:string)=>{const bare=(p:string)=>path.resolve(p.startsWith('\\\\?\\')?p.slice(4):p).toLowerCase();return bare(a)===bare(b);};
/** The first JSON object in what a command printed (a warning may come before it). */
const jsonOf=(text:string)=>JSON.parse(text.slice(text.indexOf('{'),text.lastIndexOf('}')+1));

/**
 * A plugin's manifest the way Codex finds it: .codex-plugin/plugin.json, or a plugin.json in the folder itself that names its schema
 * (Agent Plugins). A plugin.json without `$schema` is not a manifest to Codex ("missing plugin.json"), so it is not accepted here either.
 */
async function manifestOf(dir:string){
  const own=await readJson(path.join(dir,'.codex-plugin','plugin.json')).catch(e=>{if(e.code==='ENOENT')return undefined;throw Error(`.codex-plugin/plugin.json 无法解析：${e.message}`);});
  if(own)return own;
  const plain=await readJson(path.join(dir,'plugin.json')).catch(e=>{
    throw Error(e.code==='ENOENT'?'缺少 plugin.json（插件文件夹应直接包含它，注意不要多套一层）':`plugin.json 无法解析：${e.message}`);});
  if(typeof plain?.$schema!=='string')throw Error('plugin.json 没有 $schema，Codex 不会把它当作插件清单（Agent Plugins 的清单要写明 $schema）');
  return plain;
}

/** The folders under `root` that are plugins, and why the others are not. */
export async function discover(root=config.plugins):Promise<{found:Omit<Dropped,'hash'>[];skipped:Skipped[]}>{
  const found:Omit<Dropped,'hash'>[]=[],skipped:Skipped[]=[];
  let entries;
  try{entries=await fs.readdir(root,{withFileTypes:true});}catch(e){if((e as NodeJS.ErrnoException).code==='ENOENT')return {found,skipped};throw e;}
  for(const entry of entries.sort((a,b)=>a.name.localeCompare(b.name))){
    if(!entry.isDirectory()||/^[._]/.test(entry.name))continue;
    try{
      const manifest=await manifestOf(path.join(root,entry.name));
      if(typeof manifest?.name!=='string'||!NAME.test(manifest.name))throw Error('plugin.json 的 name 缺失或含有不允许的字符（只允许字母、数字、- 和 _）');
      const twin=found.find(p=>p.name===manifest.name);
      if(twin)throw Error(`与文件夹 ${twin.folder} 里的插件同名（${manifest.name}）`);
      found.push({name:manifest.name,version:typeof manifest.version==='string'?manifest.version:'',folder:entry.name});
    }catch(e){skipped.push({folder:entry.name,reason:(e as Error).message});}
  }
  return {found,skipped};
}

/** A hash of everything in a plugin folder (names and contents), so a folder dropped over an older one is noticed whatever its version says. */
export async function fingerprint(folder:string):Promise<string>{
  const hash=createHash('sha256');
  const walk=async(directory:string,prefix:string)=>{
    for(const entry of (await fs.readdir(directory,{withFileTypes:true})).sort((a,b)=>a.name<b.name?-1:1)){
      const target=path.join(directory,entry.name),rel=`${prefix}${entry.name}`;
      if(entry.isDirectory()){hash.update(`d:${rel}\0`);await walk(target,`${rel}/`);}
      else if(entry.isSymbolicLink())hash.update(`l:${rel}\0${await fs.readlink(target)}\0`);
      else{const data=await fs.readFile(target);hash.update(`f:${rel}\0${data.length}\0`);hash.update(data);}
    }
  };
  await walk(folder,'');
  return hash.digest('hex');
}

/** The stdio MCP servers a plugin declares in its mcp.json, with ${PLUGIN_ROOT} resolved to where Codex installed it. Other kinds stay as the plugin runs them. */
export async function mcpServers(installed:string):Promise<Record<string,McpServer>>{
  let declared:any;
  try{declared=await readJson(path.join(installed,'mcp.json'));}catch(e){if((e as NodeJS.ErrnoException).code==='ENOENT')return {};throw e;}
  const expand=(value:string)=>value.includes('${PLUGIN_ROOT}')?path.normalize(value.replaceAll('${PLUGIN_ROOT}',installed)):value;
  const servers:Record<string,McpServer>={};
  for(const [name,server] of Object.entries<any>(declared?.mcpServers??{})){
    if(!server||typeof server.command!=='string'||(server.type&&server.type!=='stdio'))continue;
    servers[name]={
      // The node that runs this service (it is at least the version the service needs) rather than whichever `node` the PATH of Codex's children finds.
      command:server.command==='node'?process.execPath:expand(server.command),
      args:(Array.isArray(server.args)?server.args:[]).map((x:unknown)=>expand(String(x))),
      ...(typeof server.cwd==='string'?{cwd:expand(server.cwd)}:{}),
      env:Object.fromEntries(Object.entries<any>(server.env??{}).map(([k,v])=>[k,expand(String(v))])),
    };
  }
  return servers;
}

// ---- installing

interface State {[name:string]:{hash:string;path:string}}
/** What this project has installed from plugins/, kept next to Codex's own files: whoever empties the Codex home empties this with it. */
const stateFile=(home:string)=>path.join(home,'lark-agent-plugins.json');
const readState=async(home:string):Promise<State>=>readJson(stateFile(home)).catch(()=>({}));

async function writeMarketplace(root:string,found:Omit<Dropped,'hash'>[]){
  const file=path.join(root,'.agents','plugins','marketplace.json');
  const text=JSON.stringify({name:MARKETPLACE,interface:{displayName:'lark-agent plugins'},
    plugins:found.map(p=>({name:p.name,source:{source:'local',path:`./${p.folder}`},policy:{installation:'AVAILABLE',authentication:'ON_USE'}}))},null,2)+'\n';
  if(await fs.readFile(file,'utf8').catch(()=>'')===text)return;
  await fs.mkdir(path.dirname(file),{recursive:true});await fs.writeFile(file,text);
}
/** Makes Codex's marketplace point at root; true when it had to be (re)registered, which also means nothing installed from it can be trusted. */
async function ensureMarketplace(cli:Cli,root:string):Promise<boolean>{
  const known=jsonOf(await cli(['plugin','marketplace','list','--json'])).marketplaces?.find((m:any)=>m.name===MARKETPLACE);
  if(known&&same(known.root,root))return false;
  if(known)await cli(['plugin','marketplace','remove',MARKETPLACE]);
  await cli(['plugin','marketplace','add',root]);
  return true;
}

export interface SyncResult {plugins:Plugin[];skipped:Skipped[]}
/**
 * Brings the Codex home in line with plugins/: installs what is new or changed (`plugin add` replaces the installed copy as a whole),
 * removes what was dropped before and is gone, leaves the rest alone. A plugin that cannot be installed is reported and left out;
 * the others are not held back. Never throws: without plugins the bot is what it was.
 */
export async function syncPlugins(cli:Cli,options:{root?:string;home?:string}={}):Promise<SyncResult>{
  const {root=config.plugins,home=config.codexHome}=options;
  const result:SyncResult={plugins:[],skipped:[]};
  try{
    const {found,skipped}=await discover(root);result.skipped=skipped;
    const state=await readState(home);
    // Nothing dropped and nothing ever installed by us: Codex is not even asked.
    if(!found.length&&!Object.keys(state).length)return result;
    const dropped:Dropped[]=[];
    for(const p of found)dropped.push({...p,hash:await fingerprint(path.join(root,p.folder))});
    await writeMarketplace(root,found);
    const fresh=found.length?await ensureMarketplace(cli,root):false;
    const listed:{name:string}[]=jsonOf(await cli(['plugin','list','--json'])).installed?.filter((p:any)=>p.marketplaceName===MARKETPLACE)??[];
    const next:State={};
    for(const p of dropped){
      const id=`${p.name}@${MARKETPLACE}`,before=state[p.name];
      try{
        let at=!fresh&&before?.hash===p.hash&&listed.some(l=>l.name===p.name)&&await exists(before.path)?before.path:'';
        if(!at){
          at=jsonOf(await cli(['plugin','add',id,'--json'])).installedPath;
          log.info({plugin:p.name,version:p.version,folder:p.folder},'已安装插件');
        }
        next[p.name]={hash:p.hash,path:at};
        result.plugins.push({id,name:p.name,version:p.version,path:at,mcp:await mcpServers(at)});
      }catch(e){result.skipped.push({folder:p.folder,reason:`安装失败：${safeError(e)}`});}
    }
    for(const l of listed){
      if(dropped.some(p=>p.name===l.name))continue;
      try{await cli(['plugin','remove',`${l.name}@${MARKETPLACE}`]);log.info({plugin:l.name},'已移除插件（文件夹已不在 plugins/ 里）');}
      catch(e){log.warn({plugin:l.name,error:safeError(e)},'移除插件失败');}
    }
    await fs.writeFile(stateFile(home),JSON.stringify(next,null,2));
  }catch(e){result.skipped.push({folder:'',reason:`插件同步失败：${safeError(e)}`});}
  for(const s of result.skipped)log.warn(s,'插件未加载');
  return result;
}

export interface Inspected {folder:string;name?:string;version?:string;state:'installed'|'not installed'|'changed'|'invalid';reason?:string}
/** What `start` would do with each folder in plugins/, without touching anything (for `doctor`). */
export async function inspectPlugins(options:{root?:string;home?:string}={}):Promise<Inspected[]>{
  const {root=config.plugins,home=config.codexHome}=options;
  const {found,skipped}=await discover(root),state=await readState(home);
  const checked:Inspected[]=skipped.map(s=>({folder:s.folder,state:'invalid',reason:s.reason}));
  for(const p of found){
    const before=state[p.name],hash=await fingerprint(path.join(root,p.folder));
    checked.push({folder:p.folder,name:p.name,version:p.version,state:!before?'not installed':before.hash===hash?'installed':'changed'});
  }
  return checked.sort((a,b)=>a.folder.localeCompare(b.folder));
}

// ---- what a turn is given

type Config=NonNullable<CodexOptions['config']>;
/** A name as a key of a TOML dotted path. The SDK joins path segments as they are, so one that needs quotes (a plugin id has an @) carries them itself. */
const key=(name:string)=>/^[A-Za-z0-9_-]+$/.test(name)?name:JSON.stringify(name);
/**
 * The configuration that makes plugins work for a user's turn: the plugin feature on, each plugin's own MCP servers off, and the same servers
 * registered by the host instead, so they are started with PLUGIN_ENV forwarded (a plugin's own server never sees Codex's environment).
 */
export function pluginConfig(plugins:Plugin[]):Config{
  const own:Config={},servers:Config={};
  for(const plugin of plugins){
    const off:Config={};
    for(const [name,server] of Object.entries(plugin.mcp)){
      if(servers[key(name)]){log.warn({plugin:plugin.name,server:name},'MCP 服务重名，保留先注册的那个');continue;}
      off[key(name)]={enabled:false};
      servers[key(name)]={command:server.command,args:server.args,...(server.cwd?{cwd:server.cwd}:{}),
        ...(Object.keys(server.env).length?{env:Object.fromEntries(Object.entries(server.env).map(([k,v])=>[key(k),v]))}:{}),env_vars:[...PLUGIN_ENV]};
    }
    if(Object.keys(off).length)own[key(plugin.id)]={mcp_servers:off};
  }
  return {...(Object.keys(own).length?{plugins:own}:{}),...(Object.keys(servers).length?{mcp_servers:servers}:{})};
}
