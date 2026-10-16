/**
 * How far Feishu's clock is ahead of this machine's, in ms (negative: this machine runs fast), read from the Date header of a Feishu response.
 * Message times are Feishu's, so judging them by an unadjusted local clock would drop every message once the two differ by more than the allowed delay.
 */
export async function clockOffset(fetcher:typeof fetch=fetch):Promise<number>{
  const before=Date.now();
  const response=await fetcher('https://open.feishu.cn',{method:'HEAD',redirect:'manual',signal:AbortSignal.timeout(5000)});
  const after=Date.now(),server=Date.parse(response.headers.get('date')??'');
  if(!Number.isFinite(server))throw Error('飞书响应没有时间信息');
  return server-(before+after)/2;
}
