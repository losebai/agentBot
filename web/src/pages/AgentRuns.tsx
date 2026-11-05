import { ChoiceSelect } from '../components/ChoiceSelect';
import { Alert, Button, Tag } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import { AgentApi } from '../agent-api';
import type { CatalogPage } from '../ai-types';
import type { RunSummary } from '../agent-types';
import { errorMessage } from '../api';
import { AgentRunDetail, runNames } from '../components/AgentRunDetail';
import { RecordPagination } from '../components/RecordPagination';

export function AgentRuns({token}:{token:string}) {
  const api=useMemo(()=>new AgentApi(token),[token]);const [page,setPage]=useState<CatalogPage<RunSummary>>({items:[],next_cursor:null,total:0});
  const [pageNumber,setPageNumber]=useState(1),[pageSize,setPageSize]=useState(10);
  const [session,setSession]=useState(''),[status,setStatus]=useState(''),[filters,setFilters]=useState({session_id:'',status:''}),[detail,setDetail]=useState<string>(),[error,setError]=useState(''),[busy,setBusy]=useState(false);
  /** 筛选变化或翻页时取消旧请求，页码超出删除后的范围则回到最后一页。 */
  useEffect(()=>{const controller=new AbortController();setBusy(true);api.runPage(filters,pageNumber,pageSize,controller.signal).then(value=>{if(!controller.signal.aborted){setPage(value);setError('');const last=Math.max(1,Math.ceil(value.total/pageSize));if(pageNumber>last)setPageNumber(last);}}).catch(e=>{if(!controller.signal.aborted)setError(errorMessage(e));}).finally(()=>{if(!controller.signal.aborted)setBusy(false);});return()=>controller.abort();},[api,filters,pageNumber,pageSize]);
  function refresh(){setFilters(value=>({...value}));}
  return <div className="page-content"><div className="page-heading"><div><p className="eyebrow">AGENT HISTORY</p><h1>Agent 运行记录</h1><p>查看执行状态、最终输出和工具步骤摘要。</p></div><Button loading={busy} onClick={refresh}>刷新记录</Button></div>{error&&<Alert className="notice" type="error" message={error}/>}
    <form className="record-filter" onSubmit={e=>{e.preventDefault();setPageNumber(1);setFilters({session_id:session,status});}}><label>会话 ID <input value={session} maxLength={256} onChange={e=>setSession(e.target.value)}/></label><label>状态 <ChoiceSelect aria-label="状态" value={status} onChange={e=>setStatus(e)}><option value="">全部</option>{Object.entries(runNames).map(([key,label])=><option key={key} value={key}>{label}</option>)}</ChoiceSelect></label><Button htmlType="submit" disabled={busy}>筛选</Button></form>
    <div className="panel table-wrap"><table className="data-table"><thead><tr><th>时间 / 会话</th><th>模型 / 入口</th><th>状态</th><th>用量</th><th>操作</th></tr></thead><tbody>{page.items.map(item=><tr key={item.execution_id}><td>{new Date(item.created_at.endsWith('Z')?item.created_at:item.created_at+'Z').toLocaleString()}<small>{item.session_id}</small></td><td>{item.model}<small>Python · {item.entry}</small></td><td><Tag color={item.status==='completed'?'green':item.status==='failed'?'red':'default'}>{runNames[item.status]}</Tag></td><td>{item.duration_ms} ms<small>{item.usage.total_tokens??'—'} tokens</small></td><td><Button onClick={()=>setDetail(item.execution_id)}>查看详情</Button></td></tr>)}</tbody></table>{!page.items.length&&<p className="empty-list">暂无符合条件的运行记录。</p>}</div>
    <RecordPagination current={pageNumber} pageSize={pageSize} total={page.total} loading={busy} hasNext={Boolean(page.next_cursor)} onChange={(number,size)=>{setPageNumber(number);setPageSize(size);}}/>{detail&&<AgentRunDetail api={api} id={detail} onClose={()=>setDetail(undefined)}/>}</div>;
}
