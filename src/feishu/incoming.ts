import type {IncomingMessage,IncomingAttachment} from '../types.js';

export const MAX_INPUT_FILES=10;
/** Feishu does not let a bot download a folder someone sent (none of its resource calls serves one), so the user is asked for a zip. */
export const FOLDER_HINT='飞书不允许机器人读取文件夹。请把文件夹压缩成 zip 再发送（右键文件夹 → 压缩），单个文件不超过 2 GB。';
export function incomingMessage(event:any,botId:string):IncomingMessage|undefined{
  const msg=event?.message,sender=event?.sender;
  if(sender?.sender_type!=='user'||!msg?.message_id)return;
  const isGroup=msg.chat_type==='group',mentions=Array.isArray(msg.mentions)?msg.mentions:[];
  if(isGroup&&(!botId||!mentions.some((m:any)=>m.id?.open_id===botId)))return;
  const message:IncomingMessage={tenant:event.tenant_key??sender.tenant_key??'',user:sender.sender_id?.open_id??'',
    chat:msg.chat_id,message:msg.message_id,text:'',attachments:[],
    ...(Number(msg.create_time)>0?{createdAt:Number(msg.create_time)}:{})};
  if(!message.tenant||!message.user)return;
  const attachments=message.attachments!;
  const add=(kind:IncomingAttachment['kind'],key:unknown,name:unknown)=>{
    if(typeof key!=='string'||!key)throw Error('消息中的附件缺少资源标识，请重新发送');
    if(!attachments.some(a=>a.kind===kind&&a.key===key))attachments.push({kind,key,name:typeof name==='string'&&name?name:kind==='image'?`image-${attachments.length+1}.png`:'attachment'});
  };
  try{
    const content=JSON.parse(msg.content);
    if(msg.message_type==='text')message.text=typeof content.text==='string'?content.text:'';
    else if(msg.message_type==='image')add('image',content.image_key,undefined);
    else if(msg.message_type==='file')add('file',content.file_key,content.file_name);
    // A video sent on its own; its file is downloaded like any file (the cover image beside it is not needed).
    else if(msg.message_type==='media')add('file',content.file_key,content.file_name??'video.mp4');
    else if(msg.message_type==='folder')message.error=FOLDER_HINT;
    // A Feishu voice message is an Opus recording downloaded like a file.
    else if(msg.message_type==='audio')add('file',content.file_key,'语音消息.opus');
    else if(msg.message_type==='post'){
      // Receive events normally omit the locale wrapper; accept both API representations.
      const post=Array.isArray(content.content)?content:content.zh_cn??content.en_us??Object.values(content).find((v:any)=>Array.isArray(v?.content));
      if(!post||!Array.isArray(post.content))throw Error('无法读取图文消息，请重新发送');
      const lines:string[]=[];if(typeof post.title==='string'&&post.title)lines.push(post.title);
      for(const row of post.content){
        if(!Array.isArray(row))throw Error('无法读取图文消息，请重新发送');
        const parts:string[]=[];
        for(const node of row){
          if(node.tag==='img')add('image',node.image_key,undefined);
          else if(node.tag==='media')add('file',node.file_key,node.file_name??'video.mp4');
          else if(node.tag==='file')add('file',node.file_key,node.file_name);
          else if(node.tag==='at'){
            const mention=mentions.find((m:any)=>m.key===node.user_id||m.id?.open_id===node.user_id);
            if(mention?.id?.open_id!==botId&&node.user_id!==botId)parts.push(node.user_name??node.user_id??'');
          }else if(node.tag==='a')parts.push(node.href?`${node.text??''} (${node.href})`:node.text??'');
          else if(typeof node.text==='string')parts.push(node.text);
          else if(node.tag!=='emotion')throw Error('图文消息包含暂不支持的内容，请改为文字、图片或文件');
        }
        if(parts.length)lines.push(parts.join(''));
      }
      message.text=lines.join('\n');
      // Text sent together with attachments carries them beside the content nodes, not inside them.
      for(const file of Array.isArray(post.files)?post.files:Array.isArray(content.files)?content.files:[]){
        if(file?.is_folder)throw Error(FOLDER_HINT);
        add('file',file?.file_key,file?.file_name);
      }
      for(const image of Array.isArray(post.images)?post.images:Array.isArray(content.images)?content.images:[])add('image',image?.image_key,undefined);
    }else message.error='暂不支持此消息类型，请发送文字、图片、视频、文件、语音或图文消息。';
    for(const mention of mentions){
      if(typeof mention.key!=='string')continue;
      if(mention.id?.open_id===botId)message.text=message.text.replaceAll(mention.key,'');
      else if(typeof mention.name==='string'&&mention.name)message.text=message.text.replaceAll(mention.key,`@${mention.name}`);
    }
    if(attachments.length>MAX_INPUT_FILES)throw Error(`每条消息最多接收 ${MAX_INPUT_FILES} 个附件，请分批发送`);
  }catch(e){message.error=e instanceof SyntaxError?'无法解析消息，请重新发送。':(e as Error).message;}
  if(!message.text&&!attachments.length&&!message.error)return;
  return message;
}
