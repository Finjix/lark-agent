import {config} from './config.js';
import type {IncomingMessage} from './types.js';

type AccessPolicy=Pick<typeof config,'users'|'admins'|'chats'|'tenants'>;
export function accessConfigured(policy:AccessPolicy=config){return policy.tenants.length+policy.users.length+policy.admins.length>0;}
export function authorized(message:Pick<IncomingMessage,'tenant'|'user'|'chat'>,policy:AccessPolicy=config){
  if(!message.tenant||!message.user)return false;
  // A configured tenant is a boundary for everyone, including explicitly listed users/admins.
  const member=policy.tenants.length?policy.tenants.includes(message.tenant):policy.users.includes(message.user)||policy.admins.includes(message.user);
  return member&&(!policy.chats.length||policy.chats.includes(message.chat));
}
