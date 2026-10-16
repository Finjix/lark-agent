const clock=new Intl.DateTimeFormat('en-GB',{
  timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',
  hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23',
});

export function displayName(value:string){
  return [...value.replace(/[\x00-\x1f\x7f]/g,' ').replace(/\s+/g,' ').trim()].slice(0,80).join('');
}
/** Session folder name, e.g. 20261016_161000 (Beijing time). */
export function sessionStamp(createdAt:number){
  const parts=Object.fromEntries(clock.formatToParts(new Date(createdAt)).map(p=>[p.type,p.value]));
  return `${parts.year}${parts.month}${parts.day}_${parts.hour}${parts.minute}${parts.second}`;
}
export function sessionTitle(name:string,createdAt:number){
  const user=displayName(name);if(!user)throw Error('用户显示名为空');
  const date=new Date(createdAt),parts=Object.fromEntries(clock.formatToParts(date).map(p=>[p.type,p.value]));
  const hundredths=String(Math.floor(date.getUTCMilliseconds()/10)).padStart(2,'0');
  return `${user}_${parts.year}_${parts.month}${parts.day}_${parts.hour}${parts.minute}_${parts.second}${hundredths}`;
}
