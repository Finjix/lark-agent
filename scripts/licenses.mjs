import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const packages=new Map();
async function walk(dir){
  for(const entry of await fs.readdir(dir,{withFileTypes:true})){if(entry.name==='.bin')continue;const current=path.join(dir,entry.name);
    if(entry.isDirectory())await walk(current);
    else if(entry.name==='package.json')try{const p=JSON.parse(await fs.readFile(current,'utf8'));if(p.name&&p.version)packages.set(`${p.name}@${p.version}`,{name:p.name,version:p.version,license:typeof p.license==='string'?p.license:p.license?.type??'see bundled license'});}catch{}
  }
}
await walk(path.join(root,'node_modules'));await walk(path.join(root,'runtime','node','node_modules'));
const rows=[...packages.values()].sort((a,b)=>a.name.localeCompare(b.name));
const text='# Bundled third-party software\n\nNode.js 24.21.0 is redistributed from the official Windows x64 archive; its complete license is in `runtime/node/LICENSE`. Package license files remain alongside each bundled dependency. This inventory includes development tools and the bundled npm distribution.\n\n| Package | Version | License |\n|---|---|---|\n'+rows.map(p=>`| ${p.name} | ${p.version} | ${p.license.replaceAll('|','/')} |`).join('\n')+'\n';
await fs.writeFile(path.join(root,'docs','THIRD_PARTY_NOTICES.md'),text);console.log(`License inventory: ${rows.length} packages`);
