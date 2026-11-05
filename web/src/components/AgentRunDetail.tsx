import { Tag } from 'antd';
import { useEffect, useRef, useState } from 'react';
import type { AgentApi } from '../agent-api';
import type { RunDetail, RunStep } from '../agent-types';
import { errorMessage } from '../api';
import { MarkdownRenderer } from './MarkdownRenderer';
import { MediaAttachment } from './MediaAttachment';
import { DetailDialog } from './DetailDialog';
import { AgentToolSteps } from './AgentToolSteps';
import { RecordPagination } from './RecordPagination';

export const runNames={running:'运行中',completed:'已完成',failed:'失败',cancelled:'已取消'};
/** 弹窗读取工具入参与返回值，保留模型步骤；关闭时取消请求并卸载附件。 */
export function AgentRunDetail({api,id,onClose}:{api:AgentApi;id:string;onClose:()=>void}) {
  const [detail,setDetail]=useState<RunDetail>(),[steps,setSteps]=useState<RunStep[]>([]),[total,setTotal]=useState(0),[pageNumber,setPageNumber]=useState(1),[pageSize,setPageSize]=useState(10),[error,setError]=useState(''),[busy,setBusy]=useState(false);
  const controller=useRef<AbortController>();
  useEffect(()=>{const abort=new AbortController();controller.current=abort;setBusy(true);setError('');Promise.all([detail?Promise.resolve(detail):api.getRun(id,abort.signal),api.stepPage(id,pageNumber,pageSize,abort.signal)]).then(([d,s])=>{if(!abort.signal.aborted){setDetail(d);setSteps(s.items);setTotal(s.total);const last=Math.max(1,Math.ceil(s.total/pageSize));if(pageNumber>last)setPageNumber(last);}}).catch(e=>{if(!abort.signal.aborted)setError(errorMessage(e));}).finally(()=>{if(!abort.signal.aborted)setBusy(false);});return()=>abort.abort();},[api,id,pageNumber,pageSize]);
  return <DetailDialog title="Agent 执行详情" loading={busy&&!detail} error={error} onClose={()=>{controller.current?.abort();onClose();}}>{detail&&<><p>执行标识：{id}</p><p>状态：<Tag>{runNames[detail.status]}</Tag>{detail.error_code}</p>{detail.status==='running'&&<p>运行中记录可能来自尚未结束或服务中断的请求，请结合服务状态确认。</p>}<h3>用户输入</h3><pre>{detail.prompt}</pre><h3>模型输出</h3><MarkdownRenderer content={detail.content||'没有回复正文。'}/>{(detail.attachments??[]).map(asset=><MediaAttachment key={asset.id} api={api.media} asset={asset}/>)}<h3>工具步骤</h3><AgentToolSteps steps={steps}/><ol className="agent-step-list">{steps.filter(s=>!s.tool).map(s=><li key={s.sequence}>#{s.sequence} · 第 {s.turn} 轮 · {s.kind} · {s.status} {s.summary} {s.duration_ms!==null?`${s.duration_ms} ms`:''}</li>)}</ol><RecordPagination current={pageNumber} pageSize={pageSize} total={total} loading={busy} hasNext={pageNumber*pageSize<total} onChange={(number,size)=>{setPageNumber(number);setPageSize(size);}}/></>}</DetailDialog>;
}
