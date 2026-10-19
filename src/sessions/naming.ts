export function displayName(value:string){
  return [...value.replace(/[\x00-\x1f\x7f]/g,' ').replace(/\s+/g,' ').trim()].slice(0,80).join('');
}
