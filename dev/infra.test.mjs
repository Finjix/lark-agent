import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import {Database} from '../dist/storage/sqlite.js';
import {sessionDirectory} from '../dist/desktop/adapter.js';
import {RateGate} from '../dist/feishu/delivery.js';
import {config} from '../dist/config.js';

test('session directory names cannot escape user/ or contain path separators',()=>{
  const slash=String.fromCharCode(92);
  const dir=sessionDirectory({id:'id',title:`a${slash}b:c/d..`});
  assert.equal(path.dirname(dir),config.user);assert.equal(path.basename(dir),'a_b_c_d');
  assert.equal(path.basename(sessionDirectory({id:'id',title:null})),'飞书用户会话 id');
});

test('transaction rolls back on error and nests through savepoints',()=>{
  const db=new Database(':memory:');db.exec('CREATE TABLE t(a)');
  const insert=value=>db.prepare('INSERT INTO t VALUES(?)').run(value);
  assert.throws(()=>db.transaction(()=>{insert(1);throw Error('boom');})());
  assert.equal(db.prepare('SELECT COUNT(*) AS n FROM t').get().n,0);
  db.transaction(()=>{insert(1);assert.throws(()=>db.transaction(()=>{insert(2);throw Error('inner');})());insert(3);})();
  assert.deepEqual(db.prepare('SELECT a FROM t ORDER BY a').all().map(r=>r.a),[1,3]);
  assert.equal(db.pragma('user_version',{simple:true}),0);db.close();
});

test('rate gate runs one call at a time and limits starts per interval',async()=>{
  const gate=new RateGate(2,150),starts=[];let running=0,overlap=false;
  const job=()=>gate.add(async()=>{starts.push(Date.now());if(++running>1)overlap=true;await new Promise(r=>setTimeout(r,5));running--;});
  const t0=Date.now();await Promise.all([job(),job(),job()]);await gate.onIdle();
  assert(!overlap);assert(starts[2]-starts[0]>=140,'third start waits for the window');assert(starts[1]-t0<100);
  await assert.rejects(gate.add(async()=>{throw Error('x');}),/x/);assert.equal(await gate.add(async()=>7),7);
});
