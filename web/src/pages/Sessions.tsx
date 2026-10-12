import { Alert, Button, Tag } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import { AiApi } from '../ai-api';
import { errorMessage } from '../api';
import type { Page, SessionDetail, SessionSummary } from '../ai-types';
import { MarkdownRenderer } from '../components/MarkdownRenderer';
import { AgentRuns } from './AgentRuns';
import { MediaRuns } from './MediaRuns';

const names = {completed:'已完成',failed:'失败',cancelled:'已取消'};
/** 记录类型切换时卸载详情和媒体预览。 */
export function Sessions({token}:{token:string}) {
  const [mode,setMode]=useState('plain');
  return <><div className="mode-tabs" role="tablist" aria-label="记录类型"><button role="tab" aria-selected={mode==='plain'} onClick={()=>setMode('plain')}>普通会话</button><button role="tab" aria-selected={mode==='agent'} onClick={()=>setMode('agent')}>Agent 运行记录</button><button role="tab" aria-selected={mode==='media'} onClick={()=>setMode('media')}>媒体运行记录</button></div>{mode==='agent'?<AgentRuns token={token}/>:mode==='media'?<MediaRuns token={token}/>:<OrdinarySessions token={token}/>}</>;
}
/** 保留普通文本记录分页，媒体与 Agent 记录使用独立页面。 */
function OrdinarySessions({token}:{token:string}) {
  const api=useMemo(()=>new AiApi(token),[token]);
  const [page,setPage]=useState<Page<SessionSummary>>({items:[],next_cursor:null});
  const [session,setSession]=useState('');const [status,setStatus]=useState('');
  const [filter,setFilter]=useState({session:'',status:''});const [cursor,setCursor]=useState<string>();
  const [previous,setPrevious]=useState<(string|undefined)[]>([]);const [detail,setDetail]=useState<SessionDetail>();
  const [error,setError]=useState('');const [busy,setBusy]=useState(false);
  async function load(){setBusy(true);try{setPage(await api.sessions(filter.session,filter.status,cursor));setError('');}catch(e){setError(errorMessage(e));}finally{setBusy(false);}}
  useEffect(()=>{void load();},[api,filter,cursor]);
  async function open(id:string){setBusy(true);try{setDetail(await api.session(id));setError('');}catch(e){setError(errorMessage(e));}finally{setBusy(false);}}
  return <div className="page-content"><div className="page-heading"><div><p className="eyebrow">CALL HISTORY</p><h1>会话记录</h1><p>查看 Python 普通调用的结果、状态与实际用量。</p></div><Button loading={busy} onClick={()=>void load()}>刷新记录</Button></div>{error && <Alert className="notice" type="error" showIcon message={error}/>}
    <form className="record-filter" onSubmit={e=>{e.preventDefault();setPrevious([]);setCursor(undefined);setFilter({session,status});}}><label htmlFor="record-session">会话 ID</label><input id="record-session" maxLength={256} value={session} placeholder="例如 admin_test" onChange={e=>setSession(e.target.value)}/><label htmlFor="record-status">状态</label><select id="record-status" value={status} onChange={e=>setStatus(e.target.value)}><option value="">全部</option><option value="completed">已完成</option><option value="failed">失败</option><option value="cancelled">已取消</option></select><Button htmlType="submit" disabled={busy}>筛选</Button></form>
    <div className="panel table-wrap"><table className="data-table"><thead><tr><th>时间 / 会话</th><th>模型</th><th>状态</th><th>耗时 / Token</th><th>操作</th></tr></thead><tbody>{page.items.map(item=><tr key={item.execution_id}><td>{new Date(item.created_at.endsWith('Z')?item.created_at:item.created_at+'Z').toLocaleString()}<small>{item.session_id}</small></td><td>{item.model}<small>{item.configured_model}</small></td><td><Tag color={item.status==='completed'?'green':item.status==='failed'?'red':'default'}>{names[item.status]}</Tag></td><td>{item.duration_ms} ms<small>{item.usage.total_tokens ?? '—'} tokens</small></td><td><Button disabled={busy} onClick={()=>void open(item.execution_id)}>查看详情</Button></td></tr>)}</tbody></table>{!page.items.length && <p className="empty-list">暂无符合条件的调用记录。</p>}</div>
    <div className="catalog-pagination"><Button disabled={!previous.length||busy} onClick={()=>{setCursor(previous[previous.length-1]);setPrevious(previous.slice(0,-1));}}>上一页</Button><Button disabled={!page.next_cursor||busy} onClick={()=>{setPrevious([...previous,cursor]);setCursor(page.next_cursor!);}}>下一页</Button></div>
    {detail && <section className="panel record-detail"><div className="panel-header"><h2>调用详情</h2><Button onClick={()=>setDetail(undefined)}>关闭详情</Button></div><p>请求标识：{detail.request_id}</p><p>状态：{names[detail.status]} {detail.error_code || ''}</p><h3>用户输入</h3><pre>{detail.prompt}</pre><h3>模型回复</h3><MarkdownRenderer content={detail.content || '此调用没有产生回复正文。'}/></section>}
  </div>;
}
