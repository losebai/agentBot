import type { MediaAsset } from './media-types';

const keys=['id','purpose','mime_type','size_bytes','sha256','status','created_at','expires_at'];
const types=['image/png','image/jpeg','image/webp','image/gif','audio/wav','audio/mpeg','audio/ogg','audio/flac','audio/aac','audio/pcm'];
/** 流中的附件只允许公开元数据，拒绝上游 URL、二进制和内部路径。 */
export function validateArtifact(value:unknown):asserts value is MediaAsset{
  if(!value||typeof value!=='object'||Array.isArray(value))throw new Error('附件格式无效');
  const a=value as MediaAsset;
  if(Object.keys(a).some(key=>!keys.includes(key))||keys.some(key=>!Object.hasOwn(a,key))
    ||typeof a.id!=='string'||!/^[a-f0-9]{32}$/.test(a.id)||a.purpose!=='output'||a.status!=='ready'
    ||!types.includes(a.mime_type)||!Number.isInteger(a.size_bytes)||a.size_bytes<=0||a.size_bytes>10*1024*1024
    ||typeof a.sha256!=='string'||!/^[a-f0-9]{64}$/.test(a.sha256)
    ||typeof a.created_at!=='string'||typeof a.expires_at!=='string'||!Number.isFinite(Date.parse(a.created_at))||!Number.isFinite(Date.parse(a.expires_at))||Date.parse(a.expires_at)<=Date.parse(a.created_at))throw new Error('附件格式无效');
}
