import { ChoiceSelect } from '../components/ChoiceSelect';
import { Alert, Button, Tag } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import { AgentApi } from '../agent-api';
import type { Page } from '../ai-types';
import type { RunSummary } from '../agent-types';
import { errorMessage } from '../api';
import { AgentRunDetail, runNames } from '../components/AgentRunDetail';

export function AgentRuns({token}:{token:string}) {
  const api=useMemo(()=>new AgentApi(token),[token]);const [page,setPage]=useState<Page<RunSummary>>({items:[],next_cursor:null}),[cursor,setCursor]=useState<string>(),[previous,setPrevious]=useState<(string|undefined)[]>([]);
  const [session,setSession]=useState(''),[status,setStatus]=useState(''),[filters,setFilters]=useState({session_id:'',status:''}),[detail,setDetail]=useState<string>(),[error,setError]=useState(''),[busy,setBusy]=useState(false);
  async function load(){setBusy(true);try{setPage(await api.listRuns({...filters,cursor}));setError('');}catch(e){setError(errorMessage(e));}finally{setBusy(false);}}
  useEffect(()=>{void load();},[api,filters,cursor]);
  return <div className="page-content"><div className="page-heading"><div><p className="eyebrow">AGENT HISTORY</p><h1>Agent 运行记录</h1><p>查看执行状态、最终输出和工具步骤摘要。</p></div><Button loading={busy} onClick={()=>void load()}>刷新记录</Button></div>{error&&<Alert className="notice" type="error" message={error}/>}
    <form className="record-filter" onSubmit={e=>{e.preventDefault();setCursor(undefined);setPrevious([]);setFilters({session_id:session,status});}}><label>会话 ID <input value={session} maxLength={256} onChange={e=>setSession(e.target.value)}/></label><label>状态 <ChoiceSelect aria-label="状态" value={status} onChange={e=>setStatus(e)}><option value="">全部</option>{Object.entries(runNames).map(([key,label])=><option key={key} value={key}>{label}</option>)}</ChoiceSelect></label><Button htmlType="submit" disabled={busy}>筛选</Button></form>
    <div className="panel table-wrap"><table className="data-table"><thead><tr><th>时间 / 会话</th><th>模型 / 入口</th><th>状态</th><th>用量</th><th>操作</th></tr></thead><tbody>{page.items.map(item=><tr key={item.execution_id}><td>{new Date(item.created_at.endsWith('Z')?item.created_at:item.created_at+'Z').toLocaleString()}<small>{item.session_id}</small></td><td>{item.model}<small>Python · {item.entry}</small></td><td><Tag color={item.status==='completed'?'green':item.status==='failed'?'red':'default'}>{runNames[item.status]}</Tag></td><td>{item.duration_ms} ms<small>{item.usage.total_tokens??'—'} tokens</small></td><td><Button onClick={()=>setDetail(item.execution_id)}>查看详情</Button></td></tr>)}</tbody></table>{!page.items.length&&<p className="empty-list">暂无符合条件的运行记录。</p>}</div>
    <div className="catalog-pagination"><Button disabled={busy||!previous.length} onClick={()=>{setCursor(previous.at(-1));setPrevious(previous.slice(0,-1));}}>上一页</Button><Button disabled={busy||!page.next_cursor} onClick={()=>{setPrevious([...previous,cursor]);setCursor(page.next_cursor!);}}>下一页</Button></div>{detail&&<AgentRunDetail api={api} id={detail} onClose={()=>setDetail(undefined)}/>}</div>;
}
