import { ChoiceSelect } from './ChoiceSelect';
import { Alert, Button, Tag } from 'antd';
import { useEffect, useMemo, useRef, useState } from 'react';
import { AgentApi } from '../agent-api';
import { errorMessage } from '../api';
import type { Agent, AgentEvent, AgentMetadata } from '../agent-types';
import type { ChatMessage } from '../types';
import { MarkdownRenderer } from './MarkdownRenderer';
import { MediaUpload } from './MediaUpload';
import { VideoUpload } from './VideoUpload';
import { MediaAttachment } from './MediaAttachment';
import type { MediaAsset } from '../media-types';

/** 图片仅随当前请求发送；工具附件独立于最后一轮模型回复保存。 */
export function AgentChat({token}:{token:string}) {
  const api=useMemo(()=>new AgentApi(token),[token]);const [agents,setAgents]=useState<Agent[]>([]),[cursor,setCursor]=useState<string|null>(null);
  const [code,setCode]=useState(''),[input,setInput]=useState(''),[error,setError]=useState(''),[sending,setSending]=useState(false),[streaming,setStreaming]=useState(true);
  const [session,setSession]=useState(()=>crypto.randomUUID());const [messages,setMessages]=useState<ChatMessage[]>([]),[steps,setSteps]=useState<AgentEvent[]>([]);
  const [video, setVideo] = useState<MediaAsset>();
  const [partial,setPartial]=useState<{content:string;state:string}>(),[metadata,setMetadata]=useState<AgentMetadata>();
  const [inputs,setInputs]=useState<MediaAsset[]>([]),[artifacts,setArtifacts]=useState<MediaAsset[]>([]),[consumed,setConsumed]=useState<string[]>([]),[uploading,setUploading]=useState(false);
  const controller=useRef<AbortController>(),mounted=useRef(true);
  const [maximumImages,setMaximumImages]=useState(4);
  // 数量显示跟随管理配置，硬限制仍由上传和服务端共同检查。
  useEffect(()=>{let alive=true;api.settings().then(value=>{const count=value.fields?.find(f=>f.name==='chat_max_images_per_message')?.value;if(alive&&typeof count==='number')setMaximumImages(Math.max(1,Math.min(8,count)));}).catch(()=>{});return()=>{alive=false;};},[api]);
  useEffect(()=>{mounted.current=true;api.listAgents().then(p=>{if(mounted.current){setAgents(p.items);setCursor(p.next_cursor);}}).catch(e=>{if(mounted.current)setError(errorMessage(e));});return()=>{mounted.current=false;controller.current?.abort();};},[api]);
  /** 分页补充可用 Agent。 */
  async function more(){try{const p=await api.listAgents(cursor!);if(mounted.current){setAgents([...agents,...p.items]);setCursor(p.next_cursor);}}catch(e){if(mounted.current)setError(errorMessage(e));}}
  /** 切 Agent 或新会话时让旧响应失去更新状态的资格。 */
  function reset(){controller.current?.abort();controller.current=undefined;setSending(false);setUploading(false);setInputs([]);setVideo(undefined);setConsumed([]);setArtifacts([]);setMessages([]);setSteps([]);setPartial(undefined);setMetadata(undefined);setError('');setSession(crypto.randomUUID());}
  /** 只有有效终态和 EOF 都到达才提交文字历史，附件先行保留。 */
  async function send(){
    if(sending||uploading||!code||(!input.trim()&&!inputs.length))return;
    const prompt=input.trim()||'请描述这些图片。',abort=new AbortController(),attachment_ids=inputs.map(a=>a.id);controller.current=abort;
    setConsumed(previous=>[...previous,...attachment_ids]);setSending(true);setError('');setSteps([]);setMetadata(undefined);setArtifacts([]);setPartial({content:'',state:'正在执行'});
    let content='',info:AgentMetadata|undefined;let history=messages.slice(-20);
    while(history.reduce((sum,m)=>sum+m.content.length,0)>12000)history=history.slice(2);
    // 视频独立传引用，资产说明由服务端构造，不占用用户的 2000 字输入预算。
    const body={agent_code:code,prompt,session_id:session,messages:history,attachment_ids,
      ...(video ? { video_asset_id: video.id } : {})};let generated:MediaAsset[]=[];
    try{
      if(streaming){for await(const event of api.streamAgent(body,abort.signal)){
        if(controller.current!==abort||abort.signal.aborted)throw new DOMException('已停止','AbortError');
        if(event.kind==='model_start')content='';
        if(event.kind==='text_delta')content+=event.delta||'';
        if(event.kind==='artifact'&&event.attachment)generated=[...generated,event.attachment];
        if(mounted.current){if(event.kind==='artifact')setArtifacts(generated);if(['tool_start','tool_result','warning'].includes(event.kind))setSteps(previous=>[...previous,event]);setPartial({content,state:event.kind==='tool_start'?'正在调用工具':'正在执行'});}
        if(event.kind==='error')throw new Error(`${event.message||'Agent 执行失败'} (${event.code||'AGENT_FAILED'})`);
        if(event.kind==='done')info=event.metadata;
      }if(!info)throw new Error('Agent 未返回完成事件');}
      else{const result=await api.completeAgent(body,abort.signal);content=result.content;info=result;generated=result.attachments??[];}
      if(abort.signal.aborted)throw new DOMException('已停止','AbortError');
      if(mounted.current&&controller.current===abort){setArtifacts(generated);setMessages(previous=>[...previous,{role:'user',content:prompt},{role:'assistant',content}]);setPartial(undefined);setInput('');setMetadata(info);}
    }catch(e){if(mounted.current&&controller.current===abort){setPartial({content,state:generated.length?'工具已生成，回复未完成':abort.signal.aborted?'已停止':'执行失败'});if(!abort.signal.aborted)setError(errorMessage(e));}}
    finally{if(controller.current===abort){controller.current=undefined;if(mounted.current)setSending(false);}}
  }
  return <div className="page-content chat-page"><div className="page-heading"><div><p className="eyebrow">AGENT CONVERSATION</p><h1>Agent 对话</h1><p>使用已启用的 Agent，查看模型与工具执行进度。</p></div><Button onClick={reset}>新会话</Button></div>
    <div className="chat-controls"><label htmlFor="agent-choice">选择 Agent</label><ChoiceSelect id="agent-choice" value={code} onChange={value=>{setCode(value);reset();}}><option value="">选择已启用的 Agent</option>{agents.filter(a=>a.enabled).map(a=><option key={a.id} value={a.code}>{a.name}</option>)}</ChoiceSelect>{cursor&&<Button disabled={sending} onClick={()=>void more()}>加载更多 Agent</Button>}<label className="checkbox-label"><input type="checkbox" disabled={sending} checked={streaming} onChange={e=>setStreaming(e.target.checked)}/>流式执行</label></div>
    <p className="chat-scope">管理测试可使用网页、预设 MCP 和已启用的绘图、语音工具。QQ 会话工具需在机器人会话中使用。</p>
    {error&&<Alert className="notice" type="error" showIcon message={error}/>}{metadata&&!metadata.record_saved&&<Alert className="notice" type="warning" message="回复已生成，但运行记录未保存。请检查数据库，无需重新发送。"/>}
    <div className="conversation" aria-live="polite"><div data-testid="agent-history">{messages.map((m,i)=><article className={`message ${m.role}`} key={i}><span className="message-avatar">{m.role==='user'?'你':'AB'}</span><div className="message-body"><MarkdownRenderer content={m.content}/></div></article>)}</div>
      {partial?<article className="message"><div className="message-body"><Tag>{partial.state}</Tag><MarkdownRenderer content={partial.content||'等待模型或工具返回…'}/></div></article>:!messages.length&&<div className="conversation-empty"><div className="bot-mark">AB</div><h2>让助手处理一个任务</h2><p>选择 Agent 后输入问题。</p></div>}
    </div><section className="media-results">{artifacts.map(asset=><MediaAttachment api={api.media} asset={asset} key={asset.id}/>)}</section>{steps.length>0&&<details className="agent-steps" open><summary>工具步骤 · {steps.length}</summary><ol>{steps.map(e=><li key={e.sequence}>第 {e.turn} 轮 · {e.tool||'服务提示'} · {e.kind==='tool_start'?'开始':e.kind==='warning'?'警告':e.code?'失败':'完成'} {e.duration_ms!==undefined?`${e.duration_ms} ms`:''} {e.code||''}</li>)}</ol></details>}
    <VideoUpload key={`video-${session}`} api={api.media} sessionId={session} disabled={sending || uploading} onChange={setVideo} onBusyChange={setUploading}/><MediaUpload key={session} api={api.media} sessionId={session} maximumCount={maximumImages} disabled={sending || uploading} consumedIds={consumed} onChange={setInputs} onBusyChange={setUploading}/><form className="composer" onSubmit={e=>{e.preventDefault();void send();}}><label className="sr-only" htmlFor="agent-prompt">发送给 Agent</label><textarea id="agent-prompt" rows={3} maxLength={2000} value={input} disabled={sending} placeholder="输入需要 Agent 处理的任务…" onChange={e=>setInput(e.target.value)}/><div className="composer-footer"><span>{input.length} / 2000</span>{sending?<Button aria-label="停止" onClick={()=>controller.current?.abort()}>停止</Button>:<Button type="primary" htmlType="submit" disabled={!code||uploading||(!input.trim()&&!inputs.length)}>运行 Agent</Button>}</div></form>
    {metadata&&<p className="result-metadata">Python · {metadata.model} · {metadata.duration_ms} ms · {metadata.usage?.total_tokens??'—'} tokens · 执行 {metadata.execution_id}</p>}
  </div>;
}
