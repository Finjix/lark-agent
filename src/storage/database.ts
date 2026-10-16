import {Database} from './sqlite.js';
import {randomUUID} from 'node:crypto';
import {terminal, type Task, type TaskStatus, type PendingInteraction,type IncomingAttachment,type TaskInput,type UserSession,type GeneratedImage} from '../types.js';
import {displayName,sessionTitle} from '../sessions/naming.js';

export class Store {
  db: Database; private columns?:Set<string>;
  constructor(file:string){
    this.db = new Database(file);
    this.db.pragma('journal_mode = WAL'); this.db.pragma('foreign_keys = ON'); this.db.pragma('busy_timeout = 5000');
    const version = this.db.pragma('user_version',{simple:true}) as number;
    if(version>5) throw Error('数据库版本高于当前程序支持版本');
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
        CREATE TABLE IF NOT EXISTS pending_requests (task_id TEXT NOT NULL REFERENCES tasks(id),request_id TEXT NOT NULL,
          kind TEXT NOT NULL,summary TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'PENDING',
          created_at INTEGER NOT NULL,expires_at INTEGER NOT NULL,PRIMARY KEY(task_id,request_id));
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS task_inputs (task_id TEXT NOT NULL REFERENCES tasks(id),ordinal INTEGER NOT NULL,
          kind TEXT NOT NULL, key TEXT NOT NULL,name TEXT NOT NULL,path TEXT,size INTEGER,PRIMARY KEY(task_id,ordinal));
        CREATE TABLE IF NOT EXISTS user_sessions (id TEXT PRIMARY KEY,tenant_key TEXT NOT NULL,user_id TEXT NOT NULL,
          thread_id TEXT UNIQUE,workspace_path TEXT,created_at INTEGER NOT NULL,UNIQUE(tenant_key,user_id));
        PRAGMA user_version = 4;
      `);
      if(!(this.db.prepare('PRAGMA table_info(tasks)').all() as any[]).some(c=>c.name==='session_id'))this.db.exec('ALTER TABLE tasks ADD COLUMN session_id TEXT REFERENCES user_sessions(id)');
      if(!(this.db.prepare('PRAGMA table_info(task_artifacts)').all() as any[]).some(c=>c.name==='kind'))this.db.exec("ALTER TABLE task_artifacts ADD COLUMN kind TEXT NOT NULL DEFAULT 'file'");
      const sessionColumns=new Set((this.db.prepare('PRAGMA table_info(user_sessions)').all() as any[]).map(c=>c.name));
      for(const column of ['display_name','title','applied_title'])if(!sessionColumns.has(column))this.db.exec(`ALTER TABLE user_sessions ADD COLUMN ${column} TEXT`);
      if(!sessionColumns.has('retired_at'))this.db.exec(`
        CREATE TABLE user_sessions_next (id TEXT PRIMARY KEY,tenant_key TEXT NOT NULL,user_id TEXT NOT NULL,
          thread_id TEXT UNIQUE,workspace_path TEXT,created_at INTEGER NOT NULL,
          display_name TEXT,title TEXT,applied_title TEXT,retired_at INTEGER);
        INSERT INTO user_sessions_next(id,tenant_key,user_id,thread_id,workspace_path,created_at,display_name,title,applied_title)
          SELECT id,tenant_key,user_id,thread_id,workspace_path,created_at,display_name,title,applied_title FROM user_sessions;
        DROP TABLE user_sessions;
        ALTER TABLE user_sessions_next RENAME TO user_sessions;
      `);
      this.db.exec(`CREATE UNIQUE INDEX IF NOT EXISTS current_user_session ON user_sessions(tenant_key,user_id) WHERE retired_at IS NULL;
        PRAGMA user_version = 5;`);
    })();}finally{this.db.pragma('foreign_keys = ON');}
  }
  get(id:string):Task|undefined {return this.db.prepare('SELECT * FROM tasks WHERE id=?').get(id) as Task|undefined;}
  byThread(id:string):Task|undefined {return this.db.prepare("SELECT * FROM tasks WHERE thread_id=? AND status NOT IN ('QUEUED','COMPLETED','FAILED','CANCELLED','INTERRUPTED') ORDER BY created_at DESC,rowid DESC LIMIT 1").get(id) as Task|undefined;}
  session(id:string):UserSession|undefined{return this.db.prepare('SELECT * FROM user_sessions WHERE id=?').get(id) as UserSession|undefined;}
  sessionByThread(threadId:string):UserSession|undefined{return this.db.prepare('SELECT * FROM user_sessions WHERE thread_id=? AND retired_at IS NULL').get(threadId) as UserSession|undefined;}
  sessions():UserSession[]{return this.db.prepare('SELECT * FROM user_sessions WHERE retired_at IS NULL ORDER BY created_at,rowid').all() as unknown as UserSession[];}
  nameSession(id:string,name:string){
    const session=this.session(id);if(!session)throw Error('用户会话不存在');
    const normalized=displayName(name);if(!normalized)return session;
    const title=sessionTitle(normalized,session.created_at);
    this.db.prepare('UPDATE user_sessions SET display_name=?,title=? WHERE id=?').run(normalized,title,id);
    return this.session(id)!;
  }
  sessionTitleApplied(id:string,title:string){
    this.db.prepare('UPDATE user_sessions SET applied_title=? WHERE id=? AND title=?').run(title,id,title);
  }
  sessionFor(task:Task):UserSession{
    return this.db.transaction(()=>{
      let session=this.db.prepare('SELECT * FROM user_sessions WHERE tenant_key=? AND user_id=? AND retired_at IS NULL').get(task.tenant_key,task.user_id) as UserSession|undefined;
      if(!session){this.db.prepare('INSERT INTO user_sessions(id,tenant_key,user_id,created_at) VALUES(?,?,?,?)').run(task.id,task.tenant_key,task.user_id,Date.now());session=this.session(task.id)!;}
      this.update(task.id,{session_id:session.id});return session;
    })();
  }
  renewSession(task:Task,reason:'archived'|'missing'):UserSession{
    return this.db.transaction(()=>{
      const current=this.get(task.id);if(!current||current.turn_id||this.submission(task.id))throw Error('任务已尝试提交，不能切换会话或重复执行');
      const previous=this.sessionFor(current),id=randomUUID(),createdAt=Date.now();
      this.db.prepare('UPDATE user_sessions SET retired_at=? WHERE id=?').run(createdAt,previous.id);
      this.db.prepare('INSERT INTO user_sessions(id,tenant_key,user_id,created_at) VALUES(?,?,?,?)').run(id,task.tenant_key,task.user_id,createdAt);
      if(previous.display_name)this.nameSession(id,previous.display_name);
      this.update(task.id,{session_id:id,thread_id:null,turn_id:null,workspace_path:null,last_revision:-1});
      this.event(task.id,'DESKTOP_SESSION_REPLACED',{reason,previousSessionId:previous.id,previousThreadId:previous.thread_id,sessionId:id});
      return this.session(id)!;
    })();
  }
  /** Fix the session's delivery root before any file is written, so inputs and outputs share it. */
  reserveWorkspace(id:string,directory:string):string{
    this.db.prepare('UPDATE user_sessions SET workspace_path=COALESCE(workspace_path,?) WHERE id=?').run(directory,id);
    return this.session(id)!.workspace_path!;
  }
  bindSession(id:string,threadId:string,workspace?:string){
    const session=this.session(id);if(!session)throw Error('用户会话不存在');
    if(session.thread_id&&session.thread_id!==threadId)throw Error('用户会话已经绑定其他 Desktop 聊天');
    this.db.prepare('UPDATE user_sessions SET thread_id=?,workspace_path=COALESCE(?,workspace_path) WHERE id=?').run(threadId,workspace??null,id);
  }
  list(status?:TaskStatus):Task[]{return (status?this.db.prepare('SELECT * FROM tasks WHERE status=? ORDER BY created_at,rowid').all(status):this.db.prepare('SELECT * FROM tasks ORDER BY created_at,rowid').all()) as unknown as Task[];}
  /** Started but unfinished tasks. Filtered in SQL: finished tasks are never deleted, so scanning them on every tick would keep getting slower. */
  active():Task[]{return this.db.prepare("SELECT * FROM tasks WHERE status NOT IN ('QUEUED','COMPLETED','FAILED','CANCELLED','INTERRUPTED') ORDER BY created_at,rowid").all() as unknown as Task[];}
  recent(tenant:string,user:string,limit:number):Task[]{return this.db.prepare('SELECT * FROM tasks WHERE tenant_key=? AND user_id=? ORDER BY created_at DESC,rowid DESC LIMIT ?').all(tenant,user,limit) as unknown as Task[];}
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
  images(taskId:string):GeneratedImage[]{return this.db.prepare('SELECT item_id AS id,saved_path AS savedPath,base64 FROM task_generated_images WHERE task_id=? ORDER BY rowid').all(taskId) as unknown as GeneratedImage[];}
  recordImages(taskId:string,images:GeneratedImage[]){
    return this.db.transaction(()=>{
      let changed=0;
      for(const image of images)changed+=Number(this.db.prepare(`INSERT INTO task_generated_images(task_id,item_id,saved_path,base64) VALUES(?,?,?,?)
        ON CONFLICT(task_id,item_id) DO UPDATE SET saved_path=excluded.saved_path,base64=excluded.base64
        WHERE saved_path IS NOT excluded.saved_path OR base64 IS NOT excluded.base64`).run(taskId,image.id,image.savedPath,image.base64).changes);
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
      if(terminal(status))this.db.prepare("UPDATE pending_requests SET status='EXPIRED' WHERE task_id=? AND status='PENDING'").run(id);
    })();
  }
  event(id:string,kind:string,payload:unknown){this.db.prepare('INSERT INTO task_events(task_id,kind,payload,created_at) VALUES(?,?,?,?)').run(id,kind,JSON.stringify(payload),Date.now());}
  eventPayload(id:string,kind:string):any{
    const row=this.db.prepare('SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id DESC LIMIT 1').get(id,kind) as {payload:string}|undefined;
    return row?JSON.parse(row.payload):undefined;
  }
  submission(id:string):{clientUserMessageId:string}|undefined{return this.eventPayload(id,'DESKTOP_TURN_SUBMITTING');}
  enqueue(id:string,taskId:string|null,kind:string,payload:unknown){
    this.db.prepare(`INSERT INTO delivery_outbox(id,task_id,kind,payload) VALUES(?,?,?,?)
      ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,status='PENDING',next_attempt_at=0,
      version=delivery_outbox.version+1,error=''`).run(id,taskId,kind,JSON.stringify(payload));
  }
  due():any[]{return this.db.prepare("SELECT * FROM delivery_outbox WHERE status IN ('PENDING','RETRYING') AND next_attempt_at<=? ORDER BY CASE WHEN kind='VIEW' THEN 0 ELSE 1 END,rowid LIMIT 20").all(Date.now());}
  ack(id:string,version:number){this.db.prepare("UPDATE delivery_outbox SET status='DELIVERED',error='' WHERE id=? AND version=?").run(id,version);}
  retry(id:string,version:number,error:string,permanent=false){
    this.db.prepare(`UPDATE delivery_outbox SET attempts=attempts+1,status=?,error=?,next_attempt_at=? WHERE id=? AND version=?`).run(
      permanent?'FAILED':'RETRYING',error,Date.now()+Math.min(300000,1000*2**Math.min(8,(this.db.prepare('SELECT attempts FROM delivery_outbox WHERE id=?').get(id) as any)?.attempts??0))+Math.random()*500,id,version);
  }
  messages(taskId:string):any{return this.db.prepare('SELECT * FROM task_messages WHERE task_id=?').get(taskId);}
  nextSequence(taskId:string){return this.db.prepare('UPDATE task_messages SET sequence=sequence+1 WHERE task_id=? RETURNING sequence').get(taskId) as {sequence:number};}
  pending(taskId:string):PendingInteraction[]{return this.db.prepare("SELECT request_id AS id,kind,summary,payload FROM pending_requests WHERE task_id=? AND status='PENDING'").all(taskId).map((r:any)=>({...r,payload:JSON.parse(r.payload)}));}
  setRequests(taskId:string,requests:PendingInteraction[]){
    this.db.transaction(()=>{
      for(const r of requests)this.db.prepare(`INSERT INTO pending_requests(task_id,request_id,kind,summary,payload,created_at,expires_at)
        VALUES(?,?,?,?,?,?,?) ON CONFLICT(task_id,request_id) DO UPDATE SET payload=excluded.payload,summary=excluded.summary`).run(taskId,r.id,r.kind,r.summary,JSON.stringify(r.payload),Date.now(),Date.now()+86400000);
      const unresolved=this.db.prepare("SELECT request_id AS id FROM pending_requests WHERE task_id=? AND status IN ('PENDING','RESPONDING')").all(taskId) as {id:string}[];
      for(const r of unresolved)if(!requests.some(x=>x.id===r.id))this.db.prepare("UPDATE pending_requests SET status='RESOLVED' WHERE task_id=? AND request_id=?").run(taskId,r.id);
    })();
  }
  meta(key:string,value?:unknown):any{
    if(value!==undefined)this.db.prepare('INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value').run(key,JSON.stringify(value));
    const row=this.db.prepare('SELECT value FROM metadata WHERE key=?').get(key) as any;return row?JSON.parse(row.value):undefined;
  }
  stats(){return {tasks:this.db.prepare('SELECT status,count(*) AS count FROM tasks GROUP BY status').all(),deliveries:this.db.prepare('SELECT status,count(*) AS count FROM delivery_outbox GROUP BY status').all()};}
  close(){this.db.close();}
}
