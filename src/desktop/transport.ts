import net from 'node:net';
import {EventEmitter} from 'node:events';
import {randomUUID} from 'node:crypto';

export class OutcomeUnknown extends Error {}
export class RpcRejected extends Error {}
export class FrameDecoder {
  private buffer=Buffer.alloc(0);
  constructor(private maxBytes=268435456){}
  push(chunk:Buffer):any[]{
    this.buffer=Buffer.concat([this.buffer,chunk]); const messages=[];
    while(this.buffer.length>=4){
      const size=this.buffer.readUInt32LE(0);
      if(!size||size>this.maxBytes)throw Error('Invalid Desktop frame length');
      if(this.buffer.length<size+4)break;
      messages.push(JSON.parse(this.buffer.subarray(4,size+4).toString('utf8')));this.buffer=this.buffer.subarray(size+4);
    }
    return messages;
  }
}
export function encodeFrame(message:unknown){const body=Buffer.from(JSON.stringify(message)),head=Buffer.alloc(4);head.writeUInt32LE(body.length);return Buffer.concat([head,body]);}
export class FramedPipe extends EventEmitter {
  socket?:net.Socket; private pending=new Map<string,{resolve:(m:any)=>void;reject:(e:Error)=>void;timer:NodeJS.Timeout}>();
  constructor(public path:string,private timeout=10000){super();}
  async connect(){
    const decoder=new FrameDecoder();const socket=this.socket=net.connect(this.path);
    socket.on('data',data=>{try{for(const m of decoder.push(data)){
      if(m.type==='client-discovery-request')this.send({type:'client-discovery-response',requestId:m.requestId,response:{canHandle:false}});
      const id=m.type==='response'?m.requestId:m.id;const p=id==null?undefined:this.pending.get(String(id));
      if(p&&(m.type==='response'||'result'in m||'error'in m)){this.pending.delete(String(id));clearTimeout(p.timer);
        if(m.resultType==='error'||m.error)p.reject(new RpcRejected(typeof m.error==='string'?m.error:JSON.stringify(m.error)));else p.resolve(m);}
      this.emit('message',m);
    }}catch(e){socket.destroy(e as Error);}});
    const fail=()=>{for(const p of this.pending.values()){clearTimeout(p.timer);p.reject(new OutcomeUnknown('Desktop connection lost; reconcile before retrying'));}this.pending.clear();this.emit('disconnected');};
    socket.on('error',()=>{});socket.once('close',fail);
    await new Promise<void>((resolve,reject)=>{const timer=setTimeout(()=>socket.destroy(Error('Desktop connect timeout')),this.timeout);
      socket.once('connect',()=>{clearTimeout(timer);resolve();});socket.once('error',e=>{clearTimeout(timer);reject(e);});});return this;
  }
  send(message:unknown){if(!this.socket||this.socket.destroyed)throw new OutcomeUnknown('Desktop disconnected');this.socket.write(encodeFrame(message));}
  roundTrip(message:any,timeout=this.timeout):Promise<any>{return new Promise((resolve,reject)=>{
    const id=String(message.requestId??message.id);const timer=setTimeout(()=>{this.pending.delete(id);reject(new OutcomeUnknown('Desktop RPC timed out; reconcile before retrying writes'));},timeout);
    this.pending.set(id,{resolve,reject,timer});try{this.send(message);}catch(e){clearTimeout(timer);this.pending.delete(id);reject(e);}
  });}
  close(){this.socket?.destroy();}
}
export class DesktopIPC extends FramedPipe {
  clientId='initializing-client';
  constructor(timeout=10000){super('\\\\.\\pipe\\codex-ipc',timeout);}
  override async connect(){await super.connect();const r=await this.request('initialize',{clientType:'lark-desktop-agent'},0);this.clientId=r.result.clientId;return this;}
  request(method:string,params:any,version:number,targetClientId?:string){return this.roundTrip({type:'request',requestId:randomUUID(),sourceClientId:this.clientId,method,params,version,targetClientId,timeoutMs:25000},30000);}
  follow(conversationId:string,owner:string,following=true){this.send({type:'broadcast',method:'thread-stream-following-changed',sourceClientId:this.clientId,version:1,targetClientIds:[owner],params:{hostId:'local',conversationId,following}});}
}
