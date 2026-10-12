import { Alert, Button, Tag } from 'antd';
import { useEffect, useRef, useState } from 'react';
import type { AgentApi } from '../agent-api';
import type { RunDetail, RunStep } from '../agent-types';
import { errorMessage } from '../api';
import { MarkdownRenderer } from './MarkdownRenderer';
import { MediaAttachment } from './MediaAttachment';

export const runNames={running:'运行中',completed:'已完成',failed:'失败',cancelled:'已取消'};
/** 详情只读取已保存结果，单个过期附件不妨碍查看执行步骤。 */
export function AgentRunDetail({api,id,onClose}:{api:AgentApi;id:string;onClose:()=>void}) {
  const [detail,setDetail]=useState<RunDetail>(),[steps,setSteps]=useState<RunStep[]>([]),[cursor,setCursor]=useState<string|null>(null),[error,setError]=useState(''),[busy,setBusy]=useState(false);
  const controller=useRef<AbortController>();
  useEffect(()=>{const abort=new AbortController();controller.current=abort;setBusy(true);setDetail(undefined);setSteps([]);setError('');Promise.all([api.getRun(id,abort.signal),api.listSteps(id,undefined,abort.signal)]).then(([d,s])=>{if(!abort.signal.aborted){setDetail(d);setSteps(s.items);setCursor(s.next_cursor);}}).catch(e=>{if(!abort.signal.aborted)setError(errorMessage(e));}).finally(()=>{if(!abort.signal.aborted)setBusy(false);});return()=>abort.abort();},[api,id]);
  /** 迟到的分页结果不得写入已经切换的执行详情。 */
  async function more(){const abort=controller.current!;setBusy(true);try{const p=await api.listSteps(id,cursor!,abort.signal);if(!abort.signal.aborted){setSteps([...steps,...p.items]);setCursor(p.next_cursor);}}catch(e){if(!abort.signal.aborted)setError(errorMessage(e));}finally{if(!abort.signal.aborted)setBusy(false);}}
  return <section className="panel record-detail"><div className="panel-header"><h2>Agent 执行详情</h2><Button onClick={onClose}>关闭详情</Button></div>{error&&<Alert type="error" message={error}/>}{detail&&<><p>执行标识：{id}</p><p>状态：<Tag>{runNames[detail.status]}</Tag>{detail.error_code}</p>{detail.status==='running'&&<p>运行中记录可能来自尚未结束或服务中断的请求，请结合服务状态确认。</p>}<h3>用户输入</h3><pre>{detail.prompt}</pre><h3>模型输出</h3><MarkdownRenderer content={detail.content||'没有回复正文。'}/>{(detail.attachments??[]).map(asset=><MediaAttachment key={asset.id} api={api.media} asset={asset}/>)}<h3>工具步骤</h3><ol className="agent-step-list">{steps.map(s=><li key={s.sequence}>#{s.sequence} · 第 {s.turn} 轮 · {s.tool||s.kind} · {s.status} {s.summary} {s.duration_ms!==null?`${s.duration_ms} ms`:''}</li>)}</ol>{cursor&&<Button loading={busy} onClick={()=>void more()}>加载更多步骤</Button>}</>}</section>;
}
