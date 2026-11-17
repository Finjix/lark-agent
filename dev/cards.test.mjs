import test from 'node:test';
import assert from 'node:assert/strict';
import {Store} from '../dist/storage/database.js';
import {cardJson,renderTask,limitText} from '../dist/feishu/cards.js';

function input(message='m1',user='u1',tenant='t1'){return {tenant_key:tenant,user_id:user,chat_id:'chat-'+user,source_message_id:message,instruction:'任务 '+message};}
function memory(){return new Store(':memory:');}

test('card output respects byte limits for multibyte text and contains no unrelated task content',()=>{
  const s=memory(),task=s.create(input()).task;task.result='汉🙂'.repeat(12000);task.status='COMPLETED';
  assert(Buffer.byteLength(limitText(task.result,1000))<=1000);const content=renderTask(task);assert(Buffer.byteLength(content)<=24000);
  assert.equal(cardJson(task,content).config.streaming_mode,false);s.close();
});

test('working card shows only the requested status and seconds, and drops it when execution stops',()=>{
  const s=memory(),task=s.create(input()).task;task.status='RUNNING';task.started_at=1000;
  assert.equal(cardJson(task,'',true,13500).header.title.content,'番薯搬砖中...... 12s');
  assert.equal(cardJson(task,'',true,500).header.title.content,'番薯搬砖中...... 0s');
  task.progress='正在查看图片';assert.equal(renderTask(task),'正在查看图片');
  for(const status of ['COMPLETED','FAILED','CANCELLED','INTERRUPTED']){
    task.status=status;assert.equal(cardJson(task,renderTask(task)).header,undefined);
  }
  s.close();
});
