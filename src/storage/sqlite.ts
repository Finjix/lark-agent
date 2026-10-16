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
      const nested=this.depth>0,name=`sp${this.depth}`;
      // Count the level only once BEGIN/SAVEPOINT has succeeded, so a lock timeout does not leave the depth raised.
      this.exec(nested?`SAVEPOINT ${name}`:'BEGIN IMMEDIATE');this.depth++;
      try{const result=fn();this.exec(nested?`RELEASE ${name}`:'COMMIT');return result;}
      catch(e){this.exec(nested?`ROLLBACK TO ${name}; RELEASE ${name}`:'ROLLBACK');throw e;}
      finally{this.depth--;}
    };
  }
  async backupTo(file:string){await backup(this,file);}
}
