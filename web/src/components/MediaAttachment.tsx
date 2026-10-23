import { useEffect, useState } from 'react';
import { ApiFailure, errorMessage } from '../api';
import type { MediaApi } from '../media-api';
import type { MediaAsset } from '../media-types';

/** 内容仅在认证请求完成后预览，卸载和迟到响应都释放本地 URL。 */
export function MediaAttachment({api,asset}:{api:MediaApi;asset:MediaAsset}){
  const [url,setUrl]=useState(''),[error,setError]=useState('');
  useEffect(()=>{
    const controller=new AbortController();let disposed=false,owned='';setUrl('');setError('');
    if(asset.status==='expired'){setError('附件已过期');return;}
    if(asset.status!=='ready'){setError('附件暂不可用');return;}
    api.content(asset.id,controller.signal).then(blob=>{owned=URL.createObjectURL(blob);if(disposed){URL.revokeObjectURL(owned);owned='';}else setUrl(owned);}).catch(e=>{if(!disposed)setError(e instanceof ApiFailure&&e.status===410?'附件已过期':errorMessage(e));});
    return()=>{disposed=true;controller.abort();if(owned)URL.revokeObjectURL(owned);};
  },[api,asset.id,asset.status]);
  const pcm=asset.mime_type==='audio/pcm';
  return <figure className="media-attachment">{error?<p role="status">{error}</p>:!url?<p>加载附件…</p>:<>
    {asset.mime_type.startsWith('image/')?<img src={url} alt="生成图片"/>:asset.mime_type==='video/mp4'?<video src={url} controls preload="metadata"/>:pcm?<p>PCM 需要支持原始音频的播放器，请下载后播放。</p>:<audio src={url} controls preload="metadata"/>}
    <a href={url} download={`${asset.id}.${asset.mime_type==='image/jpeg'?'jpg':asset.mime_type==='audio/mpeg'?'mp3':asset.mime_type.split('/')[1]}`}>下载附件</a></>}
    <figcaption>{asset.mime_type} · {(asset.size_bytes/1024).toFixed(1)} KiB · 保存至 {asset.expires_at.replace('T',' ')} UTC</figcaption>
  </figure>;
}
