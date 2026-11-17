import test from 'node:test';
import assert from 'node:assert/strict';
import {sanitizedFeishuError,FeishuClient,connectionDetail} from '../dist/feishu/client.js';

test('SDK failures strip request objects and authentication data from thrown errors',()=>{
  const error=sanitizedFeishuError({message:'bad',config:{headers:{Authorization:'Bearer SECRET'}},response:{data:{code:400,msg:'field validation failed',error:{field_violations:[{field:'card',description:'required'}]}}}});
  assert.equal(error.code,400);assert(error.message.includes('card'));assert(!JSON.stringify(error).includes('SECRET'));assert.equal(error.config,undefined);
});

test('Feishu connection health reads the SDK status, so quiet reconnects and terminal failures are both reported correctly',()=>{
  const f=new FeishuClient({appId:'cli_offline_test',appSecret:'offline-test-placeholder'});
  assert.equal(f.state,'idle');assert.equal(f.connected,false);
  let state='connecting';f.ws={getConnectionStatus:()=>({state}),close(){}};
  assert.equal(f.connected,false);
  state='connected';assert.equal(f.connected,true);
  // The SDK logs a handshake failure and the later success at different levels; neither may latch the flag.
  f.logger.error('[ws]','ws connect failed');f.logger.debug('[ws]','reconnect success');assert.equal(f.connected,true);
  state='reconnecting';assert.equal(f.connected,false);
  state='failed';assert.equal(f.connected,false);assert.equal(f.state,'failed');
  f.close();assert.equal(f.state,'idle');assert.equal(f.started,false);
});

test('only the long connection\'s SDK logs are kept, without the links they carry',()=>{
  assert.deepEqual(connectionDetail([['[ws]','no pong/inbound within 30s of last ping, terminating to trigger reconnect']]),
    {detail:'no pong/inbound within 30s of last ping, terminating to trigger reconnect'});
  assert.deepEqual(connectionDetail([['ws','unable to connect to the server after trying 3 times")']]),{detail:'unable to connect to the server after trying 3 times")'});
  assert.deepEqual(connectionDetail([['[ws]','get connect config success, ws url: wss://msg-frontier.feishu.cn/ws/v2?ticket=secret']]),
    {detail:'get connect config success, ws url: [URL]'});
  assert.equal(connectionDetail([['[http]','request',{headers:{authorization:'Bearer x'}}]]),undefined);
  assert.equal(connectionDetail([[{url:'x'}]]),undefined);
});
