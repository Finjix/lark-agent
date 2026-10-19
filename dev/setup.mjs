// Preloaded by `npm test`: keep per-user output directories and logs out of the project tree.
// Codex's home stays the project's own (a test checks it); whatever cleans or writes there takes explicit folders.
import os from 'node:os';
import fs from 'node:fs';
import path from 'node:path';
import {config} from '../dist/config.js';
config.user=fs.mkdtempSync(path.join(os.tmpdir(),'lark-user-'));
config.data=fs.mkdtempSync(path.join(os.tmpdir(),'lark-data-'));
process.on('exit',()=>{
  for(const directory of [config.user,config.data]){
    // The log file may still be open; what cannot be removed now stays in the temp folder.
    try{fs.rmSync(directory,{recursive:true,force:true});}catch{}
  }
});
