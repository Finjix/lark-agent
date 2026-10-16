// Preloaded by `npm test`: keep per-user output directories out of the project tree.
import os from 'node:os';
import fs from 'node:fs';
import path from 'node:path';
import {config} from '../dist/config.js';
config.user=fs.mkdtempSync(path.join(os.tmpdir(),'lark-user-'));
process.on('exit',()=>fs.rmSync(config.user,{recursive:true,force:true}));
