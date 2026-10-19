import {Database} from './sqlite.js';
import {randomUUID} from 'node:crypto';
import {terminal,BOT_DOWN, type Task, type TaskStatus, type IncomingAttachment,type TaskInput,type UserSession,type GeneratedImage} from '../types.js';
import {displayName} from '../sessions/naming.js';

const SESSION_COLUMNS=['id','tenant_key','user_id','thread_id','workspace_path','created_at','display_name','retired_at'];
export class Store {
  db: Database; private columns?:Set<string>;
  constructor(file:string){
    this.db = new Database(file);
    this.db.pragma('journal_mode = WAL'); this.db.pragma('foreign_keys = ON'); this.db.pragma('busy_timeout = 5000');
    const version = this.db.pragma('user_version',{simple:true}) as number;
    if(version>6) throw Error('数据库版本高于当前程序支持版本');
    this.db.pragma('foreign_keys = OFF');
    try{this.db.transaction(()=>{
      this.db.exec(`
        CREATE TABLE IF NOT EXISTS tasks (
          id TEXT PRIMARY KEY, tenant_key TEXT NOT NULL, user_id TEXT NOT NULL, chat_id TEXT NOT NULL,
          source_message_id TEXT NOT NULL, instruction TEXT NOT NULL, desktop_host_id TEXT NOT NULL DEFAULT 'local',
          thread_id TEXT, turn_id TEXT, workspace_path TEXT, status TEXT NOT NULL,
          result TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '', progress TEXT NOT NULL DEFAULT '',
          last_revision INTEGER NOT NULL DEFAULT -1, submission_key TEXT NOT NULL UNIQUE,
          created_at INTEGER NOT NULL, started_at INTEGER, finished_at INTEGER,
          cancel_requested INTEGER NOT NULL DEFAULT 0, resources TEXT NOT NULL DEFAULT '[]', cleaned_at INTEGER,
          UNIQUE(tenant_key,source_message_id));
        CREATE INDEX IF NOT EXISTS task_queue ON tasks(status,created_at);
        CREATE INDEX IF NOT EXISTS task_user ON tasks(tenant_key,user_id,created_at);
        CREATE UNIQUE INDEX IF NOT EXISTS task_turn ON tasks(thread_id,turn_id) WHERE turn_id IS NOT NULL;
        CREATE TABLE IF NOT EXISTS task_messages (
          task_id TEXT PRIMARY KEY REFERENCES tasks(id), message_id TEXT, card_id TEXT,
          sequence INTEGER NOT NULL DEFAULT 0, last_revision INTEGER NOT NULL DEFAULT -1,
          finalized INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS delivery_outbox (
          id TEXT PRIMARY KEY, task_id TEXT, kind TEXT NOT NULL, payload TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'PENDING', attempts INTEGER NOT NULL DEFAULT 0,
          next_attempt_at INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 1, error TEXT NOT NULL DEFAULT '');
        CREATE INDEX IF NOT EXISTS outbox_due ON delivery_outbox(status,next_attempt_at);
        CREATE TABLE IF NOT EXISTS task_events (id INTEGER PRIMARY KEY,task_id TEXT NOT NULL REFERENCES tasks(id),
          kind TEXT NOT NULL, payload TEXT NOT NULL,created_at INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS task_event_lookup ON task_events(task_id,kind,id);
        CREATE TABLE IF NOT EXISTS task_artifacts (id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES tasks(id),
          path TEXT NOT NULL,name TEXT NOT NULL,size INTEGER NOT NULL,file_key TEXT,message_id TEXT,UNIQUE(task_id,path));
        CREATE TABLE IF NOT EXISTS task_generated_images (task_id TEXT NOT NULL REFERENCES tasks(id),item_id TEXT NOT NULL,
          saved_path TEXT,base64 TEXT,PRIMARY KEY(task_id,item_id));
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS task_inputs (task_id TEXT NOT NULL REFERENCES tasks(id),ordinal INTEGER NOT NULL,
          kind TEXT NOT NULL, key TEXT NOT NULL,name TEXT NOT NULL,path TEXT,size INTEGER,PRIMARY KEY(task_id,ordinal));
        CREATE TABLE IF NOT EXISTS user_sessions (id TEXT PRIMARY KEY,tenant_key TEXT NOT NULL,user_id TEXT NOT NULL,
          thread_id TEXT UNIQUE,workspace_path TEXT,created_at INTEGER NOT NULL,display_name TEXT,retired_at INTEGER);
        CREATE TABLE IF NOT EXISTS user_keys (tenant_key TEXT NOT NULL,user_id TEXT NOT NULL,api_key TEXT NOT NULL,updated_at INTEGER NOT NULL,PRIMARY KEY(tenant_key,user_id));
        CREATE TABLE IF NOT EXISTS user_dirs (tenant_key TEXT NOT NULL,user_id TEXT NOT NULL,name TEXT NOT NULL UNIQUE COLLATE NOCASE,created_at INTEGER NOT NULL,PRIMARY KEY(tenant_key,user_id));
      `);
      if(!(this.db.prepare('PRAGMA table_info(tasks)').all() as any[]).some(c=>c.name==='session_id'))this.db.exec('ALTER TABLE tasks ADD COLUMN session_id TEXT REFERENCES user_sessions(id)');
      if(!(this.db.prepare('PRAGMA table_info(task_artifacts)').all() as any[]).some(c=>c.name==='kind'))this.db.exec("ALTER TABLE task_artifacts ADD COLUMN kind TEXT NOT NULL DEFAULT 'file'");
      // Older databases keep sessions in an earlier shape (a UNIQUE user, chat titles for Desktop); rebuild into the current one and carry the common columns over.
      const have=(this.db.prepare('PRAGMA table_info(user_sessions)').all() as any[]).map(c=>c.name as string);
      if(have.length!==SESSION_COLUMNS.length||SESSION_COLUMNS.some(c=>!have.includes(c))){
        const common=SESSION_COLUMNS.filter(c=>have.includes(c)).join(',');
        this.db.exec(`
          CREATE TABLE user_sessions_next (id TEXT PRIMARY KEY,tenant_key TEXT NOT NULL,user_id TEXT NOT NULL,
            thread_id TEXT UNIQUE,workspace_path TEXT,created_at INTEGER NOT NULL,display_name TEXT,retired_at INTEGER);
          INSERT INTO user_sessions_next(${common}) SELECT ${common} FROM user_sessions;
          DROP TABLE user_sessions;
          ALTER TABLE user_sessions_next RENAME TO user_sessions;
        `);
      }
      this.db.exec('CREATE UNIQUE INDEX IF NOT EXISTS current_user_session ON user_sessions(tenant_key,user_id) WHERE retired_at IS NULL');
      // Version 6 replaced the Desktop bridge by the Codex SDK. Interactive approvals no longer exist, and the chats of the old
      // sessions live in Desktop's own Codex home, which this service no longer reads: those sessions are retired so each user starts a fresh chat.
      this.db.exec('DROP TABLE IF EXISTS pending_requests');
      if(version>0&&version<6)this.db.prepare('UPDATE user_sessions SET retired_at=? WHERE retired_at IS NULL AND thread_id IS NOT NULL').run(Date.now());
      this.db.exec('PRAGMA user_version = 6');
    })();}finally{this.db.pragma('foreign_keys = ON');}
  }
  get(id:string):Task|undefined {return this.db.prepare('SELECT * FROM tasks WHERE id=?').get(id) as Task|undefined;}
  session(id:string):UserSession|undefined{return this.db.prepare('SELECT * FROM user_sessions WHERE id=?').get(id) as UserSession|undefined;}
  /** The API key a user set with /key; Codex runs on it. Kept as typed, in this database, which is as private as .env. */
  apiKey(tenant:string,user:string):string|undefined{return (this.db.prepare('SELECT api_key FROM user_keys WHERE tenant_key=? AND user_id=?').get(tenant,user) as {api_key:string}|undefined)?.api_key;}
  setApiKey(tenant:string,user:string,key:string){this.db.prepare('INSERT INTO user_keys(tenant_key,user_id,api_key,updated_at) VALUES(?,?,?,?) ON CONFLICT(tenant_key,user_id) DO UPDATE SET api_key=excluded.api_key,updated_at=excluded.updated_at').run(tenant,user,key,Date.now());}
  clearApiKey(tenant:string,user:string){return Number(this.db.prepare('DELETE FROM user_keys WHERE tenant_key=? AND user_id=?').run(tenant,user).changes)>0;}
  /** The user's directory name under user/: allocated once and never changed; another user with the same name gets 2, 3, ... appended. */
  userDirName(tenant:string,user:string,wanted:string):string{
    return this.db.transaction(()=>{
      const existing=this.db.prepare('SELECT name FROM user_dirs WHERE tenant_key=? AND user_id=?').get(tenant,user) as {name:string}|undefined;
      if(existing)return existing.name;
      for(let n=1;;n++){
        const name=n===1?wanted:`${wanted}${n}`;
        const taken=this.db.prepare('SELECT 1 FROM user_dirs WHERE name=?').get(name);
        if(!taken){this.db.prepare('INSERT INTO user_dirs(tenant_key,user_id,name,created_at) VALUES(?,?,?,?)').run(tenant,user,name,Date.now());return name;}
      }
    })();
  }
  sessions():UserSession[]{return this.db.prepare('SELECT * FROM user_sessions WHERE retired_at IS NULL ORDER BY created_at,rowid').all() as unknown as UserSession[];}
  nameSession(id:string,name:string){
    const session=this.session(id);if(!session)throw Error('用户会话不存在');
    const normalized=displayName(name);if(!normalized)return session;
    this.db.prepare('UPDATE user_sessions SET display_name=? WHERE id=?').run(normalized,id);
    return this.session(id)!;
  }
  sessionFor(task:Task):UserSession{
    return this.db.transaction(()=>{
      let session=this.db.prepare('SELECT * FROM user_sessions WHERE tenant_key=? AND user_id=? AND retired_at IS NULL').get(task.tenant_key,task.user_id) as UserSession|undefined;
      if(!session){this.db.prepare('INSERT INTO user_sessions(id,tenant_key,user_id,created_at) VALUES(?,?,?,?)').run(task.id,task.tenant_key,task.user_id,Date.now());session=this.session(task.id)!;}
      this.update(task.id,{session_id:session.id});return session;
    })();
  }
  /** Retires a user's chat and opens an empty one in its place; the next input starts a new Codex thread. */
  private replaceSession(previous:UserSession):UserSession{
    const id=randomUUID(),now=Date.now();
    this.db.prepare('UPDATE user_sessions SET retired_at=? WHERE id=?').run(now,previous.id);
    this.db.prepare('INSERT INTO user_sessions(id,tenant_key,user_id,created_at,display_name) VALUES(?,?,?,?,?)').run(id,previous.tenant_key,previous.user_id,now,previous.display_name);
    return this.session(id)!;
  }
  /** The chat this task was going to continue no longer exists in Codex's home; the task goes on in a new one. */
  renewSession(task:Task,reason:'missing'):UserSession{
    return this.db.transaction(()=>{
      const previous=this.sessionFor(task),next=this.replaceSession(previous);
      this.update(task.id,{session_id:next.id,thread_id:null});
      this.event(task.id,'SESSION_REPLACED',{reason,previousSessionId:previous.id,previousThreadId:previous.thread_id,sessionId:next.id});
      return next;
    })();
  }
  /** /new: forgets the conversation. False when the user has no chat yet, so there is nothing to forget. */
  resetSession(tenant:string,user:string):boolean{
    return this.db.transaction(()=>{
      const current=this.db.prepare('SELECT * FROM user_sessions WHERE tenant_key=? AND user_id=? AND retired_at IS NULL').get(tenant,user) as UserSession|undefined;
      if(!current?.thread_id)return false;this.replaceSession(current);return true;
    })();
  }
  /** The folder Codex works in for this user. */
  setSessionWorkspace(id:string,directory:string){this.db.prepare('UPDATE user_sessions SET workspace_path=? WHERE id=?').run(directory,id);}
  bindSession(id:string,threadId:string){
    const session=this.session(id);if(!session)throw Error('用户会话不存在');
    if(session.thread_id&&session.thread_id!==threadId)throw Error('用户会话已经绑定其他 Codex 会话');
    this.db.prepare('UPDATE user_sessions SET thread_id=? WHERE id=?').run(threadId,id);
  }
  list(status?:TaskStatus):Task[]{return (status?this.db.prepare('SELECT * FROM tasks WHERE status=? ORDER BY created_at,rowid').all(status):this.db.prepare('SELECT * FROM tasks ORDER BY created_at,rowid').all()) as unknown as Task[];}
  /** Started but unfinished tasks. Filtered in SQL: finished tasks are never deleted, so scanning them on every tick would keep getting slower. */
  active():Task[]{return this.db.prepare("SELECT * FROM tasks WHERE status NOT IN ('QUEUED','COMPLETED','FAILED','CANCELLED','INTERRUPTED') ORDER BY created_at,rowid").all() as unknown as Task[];}
  recent(tenant:string,user:string,limit:number):Task[]{return this.db.prepare('SELECT * FROM tasks WHERE tenant_key=? AND user_id=? ORDER BY created_at DESC,rowid DESC LIMIT ?').all(tenant,user,limit) as unknown as Task[];}
  /** True while the user has a task that has not finished (queued, starting, running or waiting): their next input is not accepted until it ends. */
  busy(tenant:string,user:string):boolean {return !!this.db.prepare("SELECT 1 FROM tasks WHERE tenant_key=? AND user_id=? AND status NOT IN ('COMPLETED','FAILED','CANCELLED','INTERRUPTED') LIMIT 1").get(tenant,user);}
  taskForMessage(tenant:string,messageId:string):Task|undefined{return this.db.prepare('SELECT * FROM tasks WHERE tenant_key=? AND source_message_id=?').get(tenant,messageId) as Task|undefined;}
  create(input:Pick<Task,'tenant_key'|'user_id'|'chat_id'|'source_message_id'|'instruction'>,resources:string[]=[],attachments:IncomingAttachment[]=[]){
    return this.db.transaction(()=>{
      const existing=this.db.prepare('SELECT * FROM tasks WHERE tenant_key=? AND source_message_id=?').get(input.tenant_key,input.source_message_id) as Task|undefined;
      if(existing)return {task:existing,created:false};
      const id=randomUUID();
      this.db.prepare(`INSERT INTO tasks(id,tenant_key,user_id,chat_id,source_message_id,instruction,status,submission_key,created_at,resources)
        VALUES(?,?,?,?,?,?,'QUEUED',?,?,?)`).run(id,input.tenant_key,input.user_id,input.chat_id,input.source_message_id,input.instruction,id,Date.now(),JSON.stringify(resources));
      this.db.prepare('INSERT INTO task_messages(task_id) VALUES(?)').run(id);
      for(const [ordinal,a] of attachments.entries())this.db.prepare('INSERT INTO task_inputs(task_id,ordinal,kind,key,name) VALUES(?,?,?,?,?)').run(id,ordinal,a.kind,a.key,a.name);
      this.event(id,'QUEUED',{}); this.enqueue(`view:${id}`,id,'VIEW',{});
      return {task:this.get(id)!,created:true};
    })();
  }
  inputs(taskId:string):TaskInput[]{return this.db.prepare('SELECT ordinal,kind,key,name,path,size FROM task_inputs WHERE task_id=? ORDER BY ordinal').all(taskId) as unknown as TaskInput[];}
  images(taskId:string):GeneratedImage[]{return this.db.prepare('SELECT item_id AS id,saved_path AS savedPath FROM task_generated_images WHERE task_id=? ORDER BY rowid').all(taskId) as unknown as GeneratedImage[];}
  recordImages(taskId:string,images:GeneratedImage[]){
    return this.db.transaction(()=>{
      let changed=0;
      for(const image of images)changed+=Number(this.db.prepare(`INSERT INTO task_generated_images(task_id,item_id,saved_path) VALUES(?,?,?)
        ON CONFLICT(task_id,item_id) DO UPDATE SET saved_path=excluded.saved_path WHERE saved_path IS NOT excluded.saved_path`).run(taskId,image.id,image.savedPath).changes);
      return changed>0;
    })();
  }
  inputReady(taskId:string,ordinal:number,path:string,size:number){this.db.prepare('UPDATE task_inputs SET path=?,size=? WHERE task_id=? AND ordinal=?').run(path,size,taskId,ordinal);}
  update(id:string,patch:Partial<Task>){
    const columns=this.columns??=new Set(this.db.prepare('PRAGMA table_info(tasks)').all().map((r:any)=>r.name as string));
    const entries=Object.entries(patch).filter(([k,v])=>columns.has(k)&&k!=='id'&&v!==undefined);
    if(entries.length)this.db.prepare(`UPDATE tasks SET ${entries.map(([k])=>`${k}=?`).join(',')} WHERE id=?`).run(...entries.map(([,v])=>v),id);
  }
  transition(id:string,status:TaskStatus,patch:Partial<Task>={}){
    this.db.transaction(()=>{
      const old=this.get(id); if(!old||terminal(old.status))return;
      this.update(id,{...patch,status,...terminal(status)?{finished_at:Date.now()}: {}});
      this.event(id,status,{}); this.enqueue(`view:${id}`,id,'VIEW',{});
    })();
  }
  event(id:string,kind:string,payload:unknown){this.db.prepare('INSERT INTO task_events(task_id,kind,payload,created_at) VALUES(?,?,?,?)').run(id,kind,JSON.stringify(payload),Date.now());}
  /** True once the user's input was handed to a Codex process; from then on the task may have changed things and is never started again. */
  submitted(id:string):boolean{return !!this.db.prepare("SELECT 1 FROM task_events WHERE task_id=? AND kind='CODEX_SUBMITTED' LIMIT 1").get(id);}
  enqueue(id:string,taskId:string|null,kind:string,payload:unknown){
    this.db.prepare(`INSERT INTO delivery_outbox(id,task_id,kind,payload) VALUES(?,?,?,?)
      ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,status='PENDING',next_attempt_at=0,
      version=delivery_outbox.version+1,error=''`).run(id,taskId,kind,JSON.stringify(payload));
  }
  /** Rows ready to send; `files` limits them to uploads (true) or everything else (false). */
  due(files?:boolean):any[]{return this.db.prepare(`SELECT * FROM delivery_outbox WHERE status IN ('PENDING','RETRYING') AND next_attempt_at<=?${files===undefined?'':` AND (kind='FILE')=${files?1:0}`} ORDER BY CASE WHEN kind='VIEW' THEN 0 ELSE 1 END,rowid LIMIT 20`).all(Date.now());}
  ack(id:string,version:number){this.db.prepare("UPDATE delivery_outbox SET status='DELIVERED',error='' WHERE id=? AND version=?").run(id,version);}
  retry(id:string,version:number,error:string,permanent=false){
    this.db.prepare(`UPDATE delivery_outbox SET attempts=attempts+1,status=?,error=?,next_attempt_at=? WHERE id=? AND version=?`).run(
      permanent?'FAILED':'RETRYING',error,Date.now()+Math.min(300000,1000*2**Math.min(8,(this.db.prepare('SELECT attempts FROM delivery_outbox WHERE id=?').get(id) as any)?.attempts??0))+Math.random()*500,id,version);
  }
  /**
   * Called once when the service starts. Whatever the previous run left behind is dead: Codex ran as a child of that process, so a task that was
   * running is over (its card is finished once, instead of staying at "running" for ever), and a message that never started is cancelled.
   * Undelivered notices and replies of finished tasks are dropped, so starting the service never speaks to users about the past.
   * Dropped rows stay as FAILED, so `retry-delivery` can still resend one on purpose.
   */
  discardLeftovers(){
    return this.db.transaction(()=>{
      const cancelled=this.db.prepare("UPDATE tasks SET status='CANCELLED',finished_at=?,error='' WHERE status='QUEUED'").run(Date.now()).changes;
      const dropped=this.db.prepare(`UPDATE delivery_outbox SET status='FAILED',error='服务重启，已丢弃重启前未送达的消息'
        WHERE status IN ('PENDING','RETRYING') AND (kind='NOTICE' OR task_id IN (SELECT id FROM tasks WHERE status IN ('COMPLETED','FAILED','CANCELLED','INTERRUPTED')))`).run().changes;
      // After the drop on purpose: the card update of an interrupted task is the one delivery that must still go out.
      const running=this.active();
      for(const task of running)this.transition(task.id,'FAILED',{error:BOT_DOWN});
      return {cancelled:Number(cancelled),dropped:Number(dropped),interrupted:running.length};
    })();
  }
  /** Finished tasks whose files are still kept and whose retention period ended before `before`. */
  expired(before:number):Task[]{return this.db.prepare("SELECT * FROM tasks WHERE cleaned_at IS NULL AND finished_at IS NOT NULL AND finished_at<? AND status IN ('COMPLETED','FAILED','CANCELLED','INTERRUPTED') ORDER BY finished_at").all(before) as unknown as Task[];}
  messages(taskId:string):any{return this.db.prepare('SELECT * FROM task_messages WHERE task_id=?').get(taskId);}
  nextSequence(taskId:string){return this.db.prepare('UPDATE task_messages SET sequence=sequence+1 WHERE task_id=? RETURNING sequence').get(taskId) as {sequence:number};}
  meta(key:string,value?:unknown):any{
    if(value!==undefined)this.db.prepare('INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value').run(key,JSON.stringify(value));
    const row=this.db.prepare('SELECT value FROM metadata WHERE key=?').get(key) as any;return row?JSON.parse(row.value):undefined;
  }
  stats(){return {tasks:this.db.prepare('SELECT status,count(*) AS count FROM tasks GROUP BY status').all(),deliveries:this.db.prepare('SELECT status,count(*) AS count FROM delivery_outbox GROUP BY status').all()};}
  close(){this.db.close();}
}
