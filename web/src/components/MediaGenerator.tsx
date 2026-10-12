import { Alert, Button, Tag } from 'antd';
import { useEffect, useMemo, useRef, useState } from 'react';
import { AiApi } from '../ai-api';
import { errorMessage } from '../api';
import { MediaApi } from '../media-api';
import type { AiModel } from '../ai-types';
import type { MediaGenerationResult } from '../media-types';
import type { ServiceStatus } from '../types';
import { MediaAttachment } from './MediaAttachment';

/** 直接媒体生成独立于文本历史，切换模式会取消当前提交。 */
export function MediaGenerator({token}:{token:string}){
  const api=useMemo(()=>new MediaApi(token),[token]),catalog=useMemo(()=>new AiApi(token),[token]);
  const [models,setModels]=useState<AiModel[]>([]),[cursor,setCursor]=useState<string|null>(null),[status,setStatus]=useState<ServiceStatus>();
  const [session,setSession]=useState(()=>crypto.randomUUID()),[kind,setKind]=useState<'IMAGE'|'AUDIO'>('IMAGE'),[model,setModel]=useState('');
  const [prompt,setPrompt]=useState(''),[format,setFormat]=useState('png'),[voice,setVoice]=useState('alloy'),[error,setError]=useState(''),[state,setState]=useState('');
  const [result,setResult]=useState<MediaGenerationResult>(),[busy,setBusy]=useState(false);
  const controller=useRef<AbortController>(),mounted=useRef(true);
  useEffect(()=>{mounted.current=true;Promise.all([catalog.models(),api.status()]).then(([page,next])=>{if(mounted.current){setModels(page.items);setCursor(page.next_cursor);setStatus(next);}}).catch(e=>{if(mounted.current)setError(errorMessage(e));});return()=>{mounted.current=false;controller.current?.abort();};},[api,catalog]);
  const available=models.filter(m=>m.enabled&&m.media_initialized&&(kind==='IMAGE'?m.media&&m.media.image_api_profile!=='none':m.media?.audio_chat_enabled));
  const selected=available.find(m=>m.alias===model),ready=status?.media_backend==='python'&&status.python_media?.storage_available;
  /** 翻页只补充目录，不中断正在生成的请求。 */
  async function more(){try{const page=await catalog.models(cursor!);if(mounted.current){setModels(previous=>[...previous,...page.items]);setCursor(page.next_cursor);}}catch(e){if(mounted.current)setError(errorMessage(e));}}
  /** 同步清除当前控制器，使旧响应无法覆盖新模式。 */
  function reset(next:'IMAGE'|'AUDIO'=kind){controller.current?.abort();controller.current=undefined;setBusy(false);setKind(next);setFormat(next==='IMAGE'?'png':'wav');setModel('');setResult(undefined);setState('');setError('');setSession(crypto.randomUUID());}
  /** 只有成功响应可以展示附件，取消保留输入便于用户自行决定下一步。 */
  async function generate(){if(busy||!selected||!prompt.trim()||!ready)return;const abort=new AbortController();controller.current=abort;setBusy(true);setError('');setResult(undefined);setState('正在生成');
    try{const value=await api.generate({model,session_id:session,prompt:prompt.trim(),output_type:kind,...(kind==='IMAGE'?{image_format:selected.media?.image_api_profile==='legacy-b64'?'png':format}:{audio_format:format,voice})},abort.signal);if(mounted.current&&controller.current===abort&&!abort.signal.aborted){setResult(value);setState('生成完成');}}
    catch(e){if(mounted.current&&controller.current===abort){setState(abort.signal.aborted?'已停止':'生成失败');if(!abort.signal.aborted)setError(errorMessage(e));}}
    finally{if(controller.current===abort){controller.current=undefined;if(mounted.current)setBusy(false);}}
  }
  return <div className="page-content chat-page"><div className="page-heading"><div><p className="eyebrow">MEDIA GENERATION</p><h1>媒体生成</h1><p>通过模型生成图片或对话音频，文件默认保存 24 小时。</p></div><Button onClick={()=>reset()}>新会话</Button></div>
    <div className="chat-scope"><Tag>{status?.media_backend==='python'?'Python 媒体':'Java 媒体'}</Tag>{!ready&&<span>请启用 Python 媒体后端，并确认媒体存储已初始化。</span>}</div>
    <div className="chat-controls"><label htmlFor="media-kind">生成类型</label><select id="media-kind" value={kind} onChange={e=>reset(e.target.value as 'IMAGE'|'AUDIO')}><option value="IMAGE">图片</option><option value="AUDIO">对话音频</option></select><label htmlFor="media-model">生成模型</label><select id="media-model" disabled={busy} value={model} onChange={e=>{setModel(e.target.value);setFormat(kind==='IMAGE'?'png':'wav');}}><option value="">选择具备对应能力的模型</option>{available.map(m=><option key={m.id} value={m.alias}>{m.alias}</option>)}</select>{cursor&&<Button onClick={()=>void more()}>加载更多模型</Button>}
      <label htmlFor="media-format">文件格式</label><select id="media-format" value={format} disabled={busy||selected?.media?.image_api_profile==='legacy-b64'&&kind==='IMAGE'} onChange={e=>setFormat(e.target.value)}>{(kind==='IMAGE'?['png','jpeg','webp']:['wav','mp3','flac','opus','aac','pcm']).map(f=><option key={f}>{f}</option>)}</select>{kind==='AUDIO'&&<><label htmlFor="media-voice">音色</label><input id="media-voice" maxLength={255} disabled={busy} value={voice} onChange={e=>setVoice(e.target.value)}/></>}
    </div>{error&&<Alert className="notice" type="error" message={error}/>}{result&&!result.record_saved&&<Alert className="notice" type="warning" message="文件已生成，但运行记录未保存。请下载附件，无需重新提交。"/>}
    <form className="composer" onSubmit={e=>{e.preventDefault();void generate();}}><label htmlFor="media-prompt">生成描述</label><textarea id="media-prompt" rows={4} value={prompt} maxLength={2000} disabled={busy} onChange={e=>setPrompt(e.target.value)}/><div className="composer-footer"><span>{prompt.length} / 2000</span>{busy?<Button danger onClick={()=>controller.current?.abort()}>停止生成</Button>:<Button type="primary" htmlType="submit" disabled={!ready||!selected||!prompt.trim()}>开始生成</Button>}</div></form>
    {state&&<p role="status">{state}</p>}{result&&<section className="media-results">{result.text&&<p>{result.text}</p>}{result.attachments.map(asset=><MediaAttachment api={api} asset={asset} key={asset.id}/>)}<p className="result-metadata">{result.model} · {result.duration_ms} ms · 执行 {result.execution_id}</p></section>}
  </div>;
}
