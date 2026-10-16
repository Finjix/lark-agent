import type {IncomingMessage} from 'node:http';
const LIMIT=64*1024;
export async function readJson(request:IncomingMessage){
  const chunks:Buffer[]=[];let size=0;
  for await(const chunk of request){size+=chunk.length;if(size>LIMIT)throw Error('请求过大');chunks.push(chunk);}
  return JSON.parse(Buffer.concat(chunks).toString('utf8'));
}
