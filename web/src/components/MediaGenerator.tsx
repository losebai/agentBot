import { ChoiceSelect } from './ChoiceSelect';
import { ModelSelect } from './ModelSelect';
import { Alert, Button, Tag } from 'antd';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useModelDirectory } from './useModelDirectory';
import { errorMessage } from '../api';
import { MediaApi } from '../media-api';
import type { MediaGenerationResult } from '../media-types';
import type { ServiceStatus } from '../types';
import { MediaAttachment } from './MediaAttachment';

/** 直接媒体生成独立于文本历史，切换模式会取消当前提交。 */
export function MediaGenerator({token}:{token:string}){
  const api=useMemo(()=>new MediaApi(token),[token]);
  const directory=useModelDirectory(token), models=directory.page.items;
  const [status,setStatus]=useState<ServiceStatus>();
  const [session,setSession]=useState(()=>crypto.randomUUID()),[kind,setKind]=useState<'IMAGE'|'AUDIO'>('IMAGE'),[model,setModel]=useState('');
  const [prompt,setPrompt]=useState(''),[format,setFormat]=useState('png'),[voice,setVoice]=useState('alloy'),[error,setError]=useState(''),[state,setState]=useState('');
  const [result,setResult]=useState<MediaGenerationResult>(),[busy,setBusy]=useState(false);
  const controller=useRef<AbortController>(),mounted=useRef(true);
  // 目录读取与生成使用独立信号，更新候选不会中断生成；离开时分别释放。
  useEffect(()=>{
    mounted.current=true;
    const request=new AbortController();
    api.status(request.signal).then(next=>{if(!request.signal.aborted)setStatus(next);})
      .catch(failure=>{if(!request.signal.aborted)setError(errorMessage(failure));});
    return()=>{mounted.current=false;request.abort();controller.current?.abort();};
  },[api]);
  const available=models.filter(m=>m.enabled&&m.media_initialized&&(kind==='IMAGE'?m.media&&m.media.image_api_profile!=='none':m.media?.audio_chat_enabled));
  const selected=available.find(m=>m.reference===model),ready=status?.python_media?.storage_available;
  /** 同步清除当前控制器，使旧响应无法覆盖新模式。 */
  function reset(next:'IMAGE'|'AUDIO'=kind){controller.current?.abort();controller.current=undefined;setBusy(false);setKind(next);setFormat(next==='IMAGE'?'png':'wav');setModel('');setResult(undefined);setState('');setError('');setSession(crypto.randomUUID());}
  /** 只有成功响应可以展示附件，取消保留输入便于用户自行决定下一步。 */
  async function generate(){if(busy||!selected||!prompt.trim()||!ready)return;const abort=new AbortController();controller.current=abort;setBusy(true);setError('');setResult(undefined);setState('正在生成');
    try{const value=await api.generate({model,session_id:session,prompt:prompt.trim(),output_type:kind,...(kind==='IMAGE'?{image_format:selected.media?.image_api_profile==='legacy-b64'?'png':format}:{audio_format:format,voice})},abort.signal);if(mounted.current&&controller.current===abort&&!abort.signal.aborted){setResult(value);setState('生成完成');}}
    catch(e){if(mounted.current&&controller.current===abort){setState(abort.signal.aborted?'已停止':'生成失败');if(!abort.signal.aborted)setError(errorMessage(e));}}
    finally{if(controller.current===abort){controller.current=undefined;if(mounted.current)setBusy(false);}}
  }
  return <div className="page-content chat-page"><div className="page-heading"><div><p className="eyebrow">MEDIA GENERATION</p><h1>媒体生成</h1><p>通过模型生成图片或对话音频，文件默认保存 24 小时。</p></div><Button onClick={()=>reset()}>新会话</Button></div>
    <div className="chat-scope"><Tag>媒体生成</Tag>{!ready&&<span>请确认媒体存储已初始化。</span>}</div>
    <div className="chat-controls"><label htmlFor="media-kind">生成类型</label><ChoiceSelect id="media-kind" value={kind} onChange={value=>reset(value as 'IMAGE'|'AUDIO')}><option value="IMAGE">图片</option><option value="AUDIO">对话音频</option></ChoiceSelect><label htmlFor="media-model">生成模型</label><ModelSelect id="media-model" models={available} value={model} disabled={busy} loading={directory.busy} onOpen={()=>void directory.load()}
      placeholder="选择具备对应能力的模型" onChange={next=>{setModel(String(next ?? ''));setFormat(kind==='IMAGE'?'png':'wav');}} />
      <label htmlFor="media-format">文件格式</label><ChoiceSelect id="media-format" value={format} disabled={busy||selected?.media?.image_api_profile==='legacy-b64'&&kind==='IMAGE'} onChange={value=>setFormat(value)}>{(kind==='IMAGE'?['png','jpeg','webp']:['wav','mp3','flac','opus','aac','pcm']).map(f=><option key={f}>{f}</option>)}</ChoiceSelect>{kind==='AUDIO'&&<><label htmlFor="media-voice">音色</label><input id="media-voice" maxLength={255} disabled={busy} value={voice} onChange={e=>setVoice(e.target.value)}/></>}
    </div>{directory.error&&<Alert className="notice" type="error" message={`模型列表读取失败：${directory.error}`}/>}
    {error&&<Alert className="notice" type="error" message={error}/>}{result&&!result.record_saved&&<Alert className="notice" type="warning" message="文件已生成，但运行记录未保存。请下载附件，无需重新提交。"/>}
    <form className="composer" onSubmit={e=>{e.preventDefault();void generate();}}><label htmlFor="media-prompt">生成描述</label><textarea id="media-prompt" rows={4} value={prompt} maxLength={2000} disabled={busy} onChange={e=>setPrompt(e.target.value)}/><div className="composer-footer"><span>{prompt.length} / 2000</span>{busy?<Button danger onClick={()=>controller.current?.abort()}>停止生成</Button>:<Button type="primary" htmlType="submit" disabled={!ready||!selected||!prompt.trim()}>开始生成</Button>}</div></form>
    {state&&<p role="status">{state}</p>}{result&&<section className="media-results">{result.text&&<p>{result.text}</p>}{result.attachments.map(asset=><MediaAttachment api={api} asset={asset} key={asset.id}/>)}<p className="result-metadata">{result.model} · {result.duration_ms} ms · 执行 {result.execution_id}</p></section>}
  </div>;
}
