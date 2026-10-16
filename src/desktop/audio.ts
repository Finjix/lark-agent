import fs from 'node:fs';
import path from 'node:path';
import {config} from '../config.js';

/** The formats Desktop itself sends as native audio input; anything else stays a path in the text. */
export const AUDIO_FILE=/\.(wav|mp3|m4a|webm|ogg)$/i;
/**
 * Desktop only sends audio to a model that lists "audio" among its input modalities. The thread's model is not known here, so audio goes in
 * natively only when every model Desktop offers accepts it; today none does, so audio files travel as paths like any other file.
 */
export function audioSupported(codexHome=config.codexHome){
  try{
    const cache=JSON.parse(fs.readFileSync(path.join(codexHome,'models_cache.json'),'utf8'));
    const offered=(Array.isArray(cache.models)?cache.models:[]).filter((m:any)=>m?.visibility==='list');
    return offered.length>0&&offered.every((m:any)=>Array.isArray(m.input_modalities)&&m.input_modalities.includes('audio'));
  }catch{return false;}
}
export const audioInputs=(inputs:{kind:string;path:string|null}[],supported=audioSupported())=>
  supported?inputs.filter(i=>i.kind==='file'&&i.path&&AUDIO_FILE.test(i.path)).map(i=>({type:'localAudio',path:i.path!})):[];
