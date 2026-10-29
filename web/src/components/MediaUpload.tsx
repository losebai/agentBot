import { useEffect, useRef, useState } from 'react';
import { errorMessage } from '../api';
import type { MediaApi } from '../media-api';
import type { MediaAsset } from '../media-types';

interface UploadItem {key:string;file:File;state:'uploading'|'ready'|'failed'|'consumed';asset?:MediaAsset;error?:string}
interface Props {api:MediaApi;sessionId:string;maximumCount:number;disabled?:boolean;consumedIds?:string[];onChange:(assets:MediaAsset[])=>void;onBusyChange?:(busy:boolean)=>void;label?:string;allowVideo?:boolean;onVideoChange?:(asset:MediaAsset|undefined)=>void}
const EMPTY:string[]=[];
/** 统一选择图片和可选投稿视频，保留原文件供重试，分别维护识图与投稿资产。 */
export function MediaUpload({api,sessionId,maximumCount,disabled,onChange,onBusyChange,consumedIds=EMPTY,label='添加图片',allowVideo=false,onVideoChange}:Props){
  const [items,setItems]=useState<UploadItem[]>([]),[error,setError]=useState('');
  const state=useRef(items),controllers=useRef(new Map<string,AbortController>()),epoch=useRef(0);
  const change=useRef(onChange),busyChange=useRef(onBusyChange),videoChange=useRef(onVideoChange);change.current=onChange;busyChange.current=onBusyChange;videoChange.current=onVideoChange;
  const consumedKey=consumedIds.join(',');
  // 使用同步引用避免同一批选择、删除和完成回调覆盖彼此的状态。
  function update(next:UploadItem[]){state.current=next;setItems(next);change.current(next.filter(i=>i.state==='ready'&&i.asset&&i.file.type!=='video/mp4').map(i=>i.asset!));videoChange.current?.(next.find(i=>i.state==='ready'&&i.file.type==='video/mp4')?.asset);busyChange.current?.(next.some(i=>i.state==='uploading'));}
  useEffect(()=>{epoch.current++;update([]);return()=>{epoch.current++;controllers.current.forEach(c=>c.abort());controllers.current.clear();};},[api,sessionId]);
  useEffect(()=>{const used=new Set(consumedIds);update(state.current.map(i=>i.asset&&used.has(i.asset.id)?{...i,state:'consumed'}:i));},[consumedKey]);
  useEffect(()=>{
    // 插件停止后取消未完成的视频上传并释放本地选择，图片仍可独立使用。
    if(!allowVideo&&state.current.some(item=>item.file.type==='video/mp4')){
      for(const item of state.current.filter(item=>item.file.type==='video/mp4')){
        controllers.current.get(item.key)?.abort();controllers.current.delete(item.key);
      }
      update(state.current.filter(item=>item.file.type!=='video/mp4'));
    }
  },[allowVideo]);
  /** 文件串行上传；视频使用独立接口，不能成为识图附件。 */
  async function upload(key:string,generation=epoch.current){
    const item=state.current.find(i=>i.key===key);if(!item||generation!==epoch.current)return;
    const abort=new AbortController();controllers.current.set(key,abort);update(state.current.map(i=>i.key===key?{...i,state:'uploading',error:undefined,asset:undefined}:i));
    try{const asset=await (item.file.type==='video/mp4'?api.uploadVideo(item.file,sessionId,abort.signal):api.upload(item.file,sessionId,abort.signal));if(generation===epoch.current&&!abort.signal.aborted)update(state.current.map(i=>i.key===key?{...i,state:'ready',asset}:i));}
    catch(e){if(generation===epoch.current&&!abort.signal.aborted)update(state.current.map(i=>i.key===key?{...i,state:'failed',error:errorMessage(e)}:i));}
    finally{if(controllers.current.get(key)===abort)controllers.current.delete(key);}
  }
  /** 客户端先检查整批限制，服务器仍是最终的文件校验边界。 */
  async function select(files:File[]){
    const maximum=Math.min(8,Math.max(1,maximumCount));setError('');
    const supported=['image/png','image/jpeg','image/webp','image/gif',...(allowVideo?['video/mp4']:[])];
    if(files.some(f=>!supported.includes(f.type))){setError(allowVideo?'请选择 PNG、JPEG、WebP、静态 GIF 图片或 MP4 视频':'请选择 PNG、JPEG、WebP 或静态 GIF 图片');return;}
    const images=files.filter(file=>file.type!=='video/mp4'),videos=files.filter(file=>file.type==='video/mp4');
    const existingImages=state.current.filter(item=>item.file.type!=='video/mp4');
    if(images.length+existingImages.length>maximum){setError(`最多 ${maximum} 张图片`);return;}
    if(images.some(f=>f.size>10*1024*1024)){setError('单张图片不能超过 10 MiB');return;}
    if([...existingImages.map(i=>i.file),...images].reduce((sum,f)=>sum+f.size,0)>20*1024*1024){setError('图片合计不能超过 20 MiB');return;}
    if(videos.length+state.current.filter(item=>item.file.type==='video/mp4').length>1){setError('最多 1 个视频，请先移除已选视频。');return;}
    if(videos.some(file=>!file.size||file.size>100*1024*1024)){setError('请选择不超过 100 MB 的 MP4 视频。');return;}
    const added:UploadItem[]=files.map(file=>({key:crypto.randomUUID(),file,state:'uploading'})),generation=epoch.current;update([...state.current,...added]);
    for(const item of added)await upload(item.key,generation);
  }
  /** 删除只取消当前浏览器请求，服务端已接收文件按保留期清理。 */
  function remove(key:string){controllers.current.get(key)?.abort();controllers.current.delete(key);update(state.current.filter(i=>i.key!==key));}
  return <div className="media-upload"><label>{label}<input type="file" accept={`image/png,image/jpeg,image/webp,image/gif${allowVideo?',video/mp4':''}`} multiple disabled={disabled||items.some(i=>i.state==='uploading')} onChange={e=>{const files=Array.from(e.target.files||[]);e.target.value='';void select(files);}}/></label>
    <p>图片仅用于本次提问，继续识图请重新选择。单张 10 MiB，合计 20 MiB。{allowVideo&&'可同时添加 1 个 MP4 投稿视频，最大 100 MB。'}</p>{error&&<p role="alert">{error}</p>}
    <ul>{items.map(i=><li key={i.key}><span>{i.file.name} · {i.state==='ready'?'已上传':i.state==='uploading'?'上传中':i.state==='consumed'?'已用于上一轮':i.error}</span>{(i.state==='failed'||i.state==='consumed')&&<button type="button" disabled={disabled||items.some(item=>item.state==='uploading')} onClick={()=>void upload(i.key)}>{i.state==='failed'?'重试':'重新上传'} {i.file.name}</button>}<button type="button" disabled={disabled} aria-label={`移除 ${i.file.name}`} onClick={()=>remove(i.key)}>移除</button></li>)}</ul>
  </div>;
}
