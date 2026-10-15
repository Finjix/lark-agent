import pino from 'pino';
import {mkdirSync} from 'node:fs';
import path from 'node:path';
import {config} from './config.js';
const directory=path.join(config.data,'logs');mkdirSync(directory,{recursive:true});
const destination=pino.destination({dest:path.join(directory,`${new Date().toISOString().slice(0,10)}-${process.pid}.jsonl`),sync:false});
export const log = pino({level:process.env.LOG_LEVEL || 'info', redact:{paths:['secret','token','appSecret','headers','request.headers','config.FEISHU_APP_SECRET'],censor:'[REDACTED]'}},pino.multistream([{stream:process.stdout},{stream:destination}]));
export const safeError = (error:unknown) => {
  let text=error instanceof Error?error.message:'未知错误';text=text.replace(/(Bearer\s+)[^\s]+/gi,'$1[REDACTED]');
  if(config.FEISHU_APP_SECRET)text=text.replaceAll(config.FEISHU_APP_SECRET,'[REDACTED]');return text;
};
