import { ModelSelect, selectedModel } from '../components/ModelSelect';
import { Alert, Button, Input, Tag } from 'antd';
import { DeleteOutlined, SendOutlined } from '@ant-design/icons';
import { useEffect, useMemo, useRef, useState } from 'react';
import { ApiClient, errorMessage } from '../api';
import { useModelDirectory } from '../components/useModelDirectory';
import { MarkdownRenderer } from '../components/MarkdownRenderer';
import type { CompletionMetadata } from '../ai-types';
import type { ChatMessage, ServiceStatus } from '../types';
import { AgentChat } from '../components/AgentChat';
import { MediaGenerator } from '../components/MediaGenerator';

function newSession() {return Array.from(crypto.getRandomValues(new Uint8Array(12)),byte=>byte.toString(16).padStart(2,'0')).join('');}
/** 每次切换卸载旧模式，释放执行、上传和附件预览。 */
export function Chat({token}:{token:string}) {
  const [mode,setMode]=useState('plain');
  return <><div className="mode-tabs" role="tablist" aria-label="对话模式"><button role="tab" aria-selected={mode==='plain'} onClick={()=>setMode('plain')}>普通对话</button><button role="tab" aria-selected={mode==='agent'} onClick={()=>setMode('agent')}>Agent 对话</button><button role="tab" aria-selected={mode==='media'} onClick={()=>setMode('media')}>媒体生成</button></div>{mode==='agent'?<AgentChat token={token}/>:mode==='media'?<MediaGenerator token={token}/>:<OrdinaryChat token={token}/>}</>;
}
/** 普通对话保留默认配置绑定，目录只负责展示供应商和模型名。 */
function OrdinaryChat({token}:{token:string}) {
  const api=useMemo(()=>new ApiClient(token),[token]);
  const [session,setSession]=useState(newSession);const [messages,setMessages]=useState<ChatMessage[]>([]);
  const [input,setInput]=useState('');const [error,setError]=useState('');const [sending,setSending]=useState(false);
  const [status,setStatus]=useState<ServiceStatus>();
  const directory=useModelDirectory(token), models=directory.page.items;
  const [model,setModel]=useState('');const [streaming,setStreaming]=useState(false);
  const [partial,setPartial]=useState<{prompt:string;content:string;state:string}>();const [metadata,setMetadata]=useState<CompletionMetadata>();
  const controller=useRef<AbortController>();const mounted=useRef(true);
  const configuredModel = selectedModel(models, status?.configured_model ?? '');
  // 服务状态与目录独立读取；打开模型选择框只更新目录，不清空对话和测试模型。
  useEffect(()=>{
    mounted.current=true;
    const request=new AbortController();
    api.status(request.signal).then(value=>{if(!request.signal.aborted)setStatus(value);})
      .catch(failure=>{if(!request.signal.aborted)setError(errorMessage(failure));});
    return()=>{mounted.current=false;request.abort();controller.current?.abort();};
  },[api]);
  async function send(){
    if(!input.trim()||sending)return;
    const prompt=input.trim(), abort=new AbortController();controller.current=abort;
    setSending(true);setError('');setMetadata(undefined);setPartial({prompt,content:'',state:'正在生成'});
    let history=messages.slice(-20);while(history.reduce((sum,item)=>sum+item.content.length,0)>12000)history=history.slice(2);
    let content='', completed=false, info:CompletionMetadata|undefined;
    try{
      if(streaming){
        for await(const event of api.streamChat({prompt,session_id:session,messages:history,...(model?{model}:{})},abort.signal)){
          if(event.kind==='delta'){content+=event.delta || '';if(mounted.current)setPartial({prompt,content,state:'正在生成'});}
          if(event.kind==='error')throw new Error(event.message || '对话流未完成');
          if(event.kind==='done'){completed=true;info=event.metadata;controller.current=undefined;}
        }
        if(!completed)throw new Error('对话流未完成');
      }else{const response=await api.chat(prompt,session,history,model||undefined,abort.signal);content=response.content;info=response.metadata || undefined;completed=true;controller.current=undefined;}
      if(mounted.current){setMessages(previous=>[...previous,{role:'user',content:prompt},{role:'assistant',content}]);setPartial(undefined);setInput('');setMetadata(info);}
    }catch(e){if(mounted.current){setPartial({prompt,content,state:abort.signal.aborted?'已停止':'生成失败'});if(!abort.signal.aborted)setError(errorMessage(e));}}
    finally{if(controller.current===abort)controller.current=undefined;if(mounted.current)setSending(false);}
  }
  return <div className="page-content chat-page"><div className="page-heading"><div><p className="eyebrow">CONVERSATION</p><h1>对话测试</h1><p>验证普通文本回复与流式输出。</p></div><div className="heading-actions"><Button icon={<DeleteOutlined aria-hidden/>} disabled={sending||(!messages.length&&!partial)} onClick={()=>{setMessages([]);setPartial(undefined);setMetadata(undefined);setSession(newSession());setError('');}}>新会话</Button></div></div>
    <div className="chat-scope"><Tag>普通对话</Tag><span>消息只用于测试，不会发送到 QQ。</span></div>
    <div className="chat-controls"><><label htmlFor="chat-model">测试模型</label><ModelSelect id="chat-model" models={models} value={model} nullable disabled={sending} loading={directory.busy} onOpen={()=>void directory.load()}
      placeholder={configuredModel ? `使用配置模型 ${configuredModel.upstream_model}` : '使用配置模型'} onChange={next => setModel(String(next ?? ''))} /><label className="checkbox-label"><input type="checkbox" disabled={sending} checked={streaming} onChange={e=>setStreaming(e.target.checked)}/>流式输出</label></></div>
    {directory.error&&<Alert type="error" showIcon className="notice" message={`模型列表读取失败：${directory.error}`}/>}
    {error&&<Alert type="error" showIcon className="notice" message={error}/>}{metadata&&!metadata.record_saved&&<Alert className="notice" type="warning" showIcon message="回复已生成，但会话记录未保存。请检查数据库，无需重新发送。"/>}
    <div className="conversation" aria-live="polite">{!messages.length&&!partial&&<div className="conversation-empty"><div className="bot-mark">TB</div><h2>开始一段对话</h2><p>发送消息，检查模型配置与服务连接。</p></div>}{messages.map((message,index)=><article className={`message ${message.role}`} key={index}><div className="message-avatar">{message.role==='user'?'你':'TB'}</div><div className="message-body"><span className="message-name">{message.role==='user'?'你':'taobot'}</span><MarkdownRenderer content={message.content}/></div></article>)}
      {partial&&<><article className="message user"><div className="message-avatar">你</div><div className="message-body"><MarkdownRenderer content={partial.prompt}/></div></article><article className="message assistant"><div className="message-avatar">TB</div><div className="message-body"><Tag>{partial.state}</Tag><MarkdownRenderer content={partial.content || (sending?'正在等待模型回复…':'此轮没有生成正文。')}/></div></article></>}
    </div><div className="composer"><label className="sr-only" htmlFor="chat-message">消息</label><Input.TextArea id="chat-message" value={input} maxLength={2000} disabled={sending} placeholder="输入消息…" autoSize={{minRows:2,maxRows:6}} onChange={e=>setInput(e.target.value)} onKeyDown={e=>{if(e.key==='Enter'&&(e.ctrlKey||e.metaKey)){e.preventDefault();void send();}}}/><div className="composer-footer"><span>{input.length}/2000 · Ctrl + Enter 发送</span>{sending?<Button danger onClick={()=>controller.current?.abort()}>停止生成</Button>:<Button type="primary" icon={<SendOutlined aria-hidden/>} disabled={!input.trim()} onClick={()=>void send()}>发送消息</Button>}</div></div>
    {metadata&&<p className="result-metadata">{metadata.model} · {metadata.duration_ms} ms · {metadata.usage.total_tokens ?? '—'} tokens · 请求 {metadata.request_id}</p>}
  </div>;
}
