import { useEffect, useRef, useState } from 'react';
import { errorMessage } from '../api';
import type { MediaApi } from '../media-api';
import type { MediaAsset } from '../media-types';

interface UploadItem {key:string;file:File;state:'uploading'|'ready'|'failed'|'consumed';asset?:MediaAsset;error?:string}
interface Props {api:MediaApi;sessionId:string;maximumCount:number;disabled?:boolean;consumedIds?:string[];onChange:(assets:MediaAsset[])=>void;onBusyChange?:(busy:boolean)=>void}
const EMPTY:string[]=[];
/** 保留原 File 供用户逐项重试，已绑定的 ID 不能进入下一轮请求。 */
export function MediaUpload({api,sessionId,maximumCount,disabled,onChange,onBusyChange,consumedIds=EMPTY}:Props){
  const [items,setItems]=useState<UploadItem[]>([]),[error,setError]=useState('');
  const state=useRef(items),controllers=useRef(new Map<string,AbortController>()),epoch=useRef(0);
  const change=useRef(onChange),busyChange=useRef(onBusyChange);change.current=onChange;busyChange.current=onBusyChange;
  const consumedKey=consumedIds.join(',');
  // 使用同步引用避免同一批选择、删除和完成回调覆盖彼此的状态。
  function update(next:UploadItem[]){state.current=next;setItems(next);change.current(next.filter(i=>i.state==='ready'&&i.asset).map(i=>i.asset!));busyChange.current?.(next.some(i=>i.state==='uploading'));}
  useEffect(()=>{epoch.current++;update([]);return()=>{epoch.current++;controllers.current.forEach(c=>c.abort());controllers.current.clear();};},[api,sessionId]);
  useEffect(()=>{const used=new Set(consumedIds);update(state.current.map(i=>i.asset&&used.has(i.asset.id)?{...i,state:'consumed'}:i));},[consumedKey]);
  /** 串行接收文件，避免一次选择多图就占满服务端两个上传槽。 */
  async function upload(key:string,generation=epoch.current){
    const item=state.current.find(i=>i.key===key);if(!item||generation!==epoch.current)return;
    const abort=new AbortController();controllers.current.set(key,abort);update(state.current.map(i=>i.key===key?{...i,state:'uploading',error:undefined,asset:undefined}:i));
    try{const asset=await api.upload(item.file,sessionId,abort.signal);if(generation===epoch.current&&!abort.signal.aborted)update(state.current.map(i=>i.key===key?{...i,state:'ready',asset}:i));}
    catch(e){if(generation===epoch.current&&!abort.signal.aborted)update(state.current.map(i=>i.key===key?{...i,state:'failed',error:errorMessage(e)}:i));}
    finally{if(controllers.current.get(key)===abort)controllers.current.delete(key);}
  }
  /** 客户端先检查整批限制，服务器仍是最终的文件校验边界。 */
  async function select(files:File[]){
    const maximum=Math.min(8,Math.max(1,maximumCount));setError('');
    if(files.length+state.current.length>maximum){setError(`最多 ${maximum} 张图片`);return;}
    if(files.some(f=>f.size>10*1024*1024)){setError('单张图片不能超过 10 MiB');return;}
    if([...state.current.map(i=>i.file),...files].reduce((sum,f)=>sum+f.size,0)>20*1024*1024){setError('图片合计不能超过 20 MiB');return;}
    if(files.some(f=>!['image/png','image/jpeg','image/webp','image/gif'].includes(f.type))){setError('请选择 PNG、JPEG、WebP 或静态 GIF 图片');return;}
    const added:UploadItem[]=files.map(file=>({key:crypto.randomUUID(),file,state:'uploading'})),generation=epoch.current;update([...state.current,...added]);
    for(const item of added)await upload(item.key,generation);
  }
  /** 删除只取消当前浏览器请求，服务端已接收文件按保留期清理。 */
  function remove(key:string){controllers.current.get(key)?.abort();controllers.current.delete(key);update(state.current.filter(i=>i.key!==key));}
  return <div className="media-upload"><label>添加图片<input type="file" accept="image/png,image/jpeg,image/webp,image/gif" multiple disabled={disabled||items.some(i=>i.state==='uploading')} onChange={e=>{const files=Array.from(e.target.files||[]);e.target.value='';void select(files);}}/></label>
    <p>图片仅用于本次提问，继续识图请重新选择。单张 10 MiB，合计 20 MiB。</p>{error&&<p role="alert">{error}</p>}
    <ul>{items.map(i=><li key={i.key}><span>{i.file.name} · {i.state==='ready'?'已上传':i.state==='uploading'?'上传中':i.state==='consumed'?'已用于上一轮':i.error}</span>{(i.state==='failed'||i.state==='consumed')&&<button type="button" disabled={disabled||items.some(item=>item.state==='uploading')} onClick={()=>void upload(i.key)}>{i.state==='failed'?'重试':'重新上传'} {i.file.name}</button>}<button type="button" disabled={disabled} aria-label={`移除 ${i.file.name}`} onClick={()=>remove(i.key)}>移除</button></li>)}</ul>
  </div>;
}
