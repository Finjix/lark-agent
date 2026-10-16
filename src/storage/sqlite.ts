import {DatabaseSync,backup,type DatabaseSyncOptions} from 'node:sqlite';

/** node:sqlite plus the three helpers the store relies on: pragma, nestable transaction, backup. */
export class Database extends DatabaseSync {
  private depth=0;
  constructor(file:string,options?:DatabaseSyncOptions){if(options)super(file,options);else super(file);}
  pragma(statement:string,options?:{simple?:boolean}):unknown{
    const rows=this.prepare(`PRAGMA ${statement}`).all() as Record<string,unknown>[];
    return options?.simple?Object.values(rows[0]??{})[0]:rows;
  }
  /** Returns a function that runs `fn` atomically; nested calls become savepoints. */
  transaction<T>(fn:()=>T):()=>T{
    return ()=>{
      const name=`sp${this.depth++}`;
      this.exec(this.depth===1?'BEGIN IMMEDIATE':`SAVEPOINT ${name}`);
      try{const result=fn();this.exec(this.depth===1?'COMMIT':`RELEASE ${name}`);return result;}
      catch(e){this.exec(this.depth===1?'ROLLBACK':`ROLLBACK TO ${name}; RELEASE ${name}`);throw e;}
      finally{this.depth--;}
    };
  }
  async backupTo(file:string){await backup(this,file);}
}
