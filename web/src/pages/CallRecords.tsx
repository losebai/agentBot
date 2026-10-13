import { Alert, Button, Tag } from 'antd';
import { useEffect, useMemo, useRef, useState } from 'react';
import { AgentApi } from '../agent-api';
import { errorMessage } from '../api';
import { CallRecordsApi } from '../call-records-api';
import type { CallDetail, CallFilters, CallSummary } from '../call-records-api';
import type { Page } from '../ai-types';
import { AgentRunDetail } from '../components/AgentRunDetail';
import { MarkdownRenderer } from '../components/MarkdownRenderer';

const types = {ordinary: '普通调用', agent: 'Agent 调用', auto_speak: '自动发言决策', unknown: '历史未知类型'};
const statuses: Record<string, string> = {running: '运行中', completed: '已完成', failed: '失败', cancelled: '已取消', unknown: '未知'};
const initial: CallFilters = {type: '', session_id: '', status: ''};

/** 普通调用与 Agent 共用列表；类型筛选不会触发模型重放。 */
export function CallRecords({token}: {token: string}) {
  const api = useMemo(() => new CallRecordsApi(token), [token]);
  const agent = useMemo(() => new AgentApi(token), [token]);
  const [draft, setDraft] = useState(initial), [filters, setFilters] = useState(initial);
  const [page, setPage] = useState<Page<CallSummary>>({items: [], next_cursor: null});
  const [cursor, setCursor] = useState<string>(), [previous, setPrevious] = useState<(string | undefined)[]>([]);
  const [detail, setDetail] = useState<CallDetail>(), [steps, setSteps] = useState<string>();
  const [error, setError] = useState(''), [busy, setBusy] = useState(false), [refresh, setRefresh] = useState(0);
  const detailRequest = useRef<AbortController>();

  useEffect(() => {
    const controller = new AbortController();
    detailRequest.current?.abort(); setDetail(undefined); setSteps(undefined); setBusy(true);
    // 切换筛选时丢弃迟到的列表响应，避免页码与内容不一致。
    api.list(filters, cursor, controller.signal).then(result => {
      if (!controller.signal.aborted) { setPage(result); setError(''); }
    }).catch(e => { if (!controller.signal.aborted) setError(errorMessage(e)); })
      .finally(() => { if (!controller.signal.aborted) setBusy(false); });
    return () => { controller.abort(); detailRequest.current?.abort(); };
  }, [api, filters, cursor, refresh]);

  /** 详情请求后发优先，关闭或切换记录时停止旧响应。 */
  async function open(id: number) {
    detailRequest.current?.abort();
    const controller = new AbortController(); detailRequest.current = controller;
    setDetail(undefined); setSteps(undefined);
    try {
      const result = await api.detail(id, controller.signal);
      if (!controller.signal.aborted) { setDetail(result); setError(''); }
    } catch (e) { if (!controller.signal.aborted) setError(errorMessage(e)); }
  }
  return <div className="page-content">
    <div className="page-heading"><div><p className="eyebrow">CALL HISTORY</p><h1>调用记录</h1><p>统一查看普通调用、Agent 调用和迁入的历史记录。</p></div><Button loading={busy} onClick={() => setRefresh(value => value + 1)}>刷新记录</Button></div>
    {error && <Alert className="notice" type="error" message={error} />}
    <form className="record-filter" onSubmit={e => { e.preventDefault(); setPrevious([]); setCursor(undefined); setFilters({...draft}); }}>
      <label>类型 <select value={draft.type} onChange={e => setDraft({...draft, type: e.target.value})}><option value="">全部类型</option>{Object.entries(types).map(([key, name]) => <option key={key} value={key}>{name}</option>)}</select></label>
      <label>会话 ID <input value={draft.session_id} maxLength={255} onChange={e => setDraft({...draft, session_id: e.target.value})} /></label>
      <label>状态 <select value={draft.status} onChange={e => setDraft({...draft, status: e.target.value})}><option value="">全部状态</option>{Object.entries(statuses).map(([key, name]) => <option key={key} value={key}>{name}</option>)}</select></label>
      <Button htmlType="submit" disabled={busy}>筛选</Button>
    </form>
    <div className="panel table-wrap"><table className="data-table"><thead><tr><th>ID / 时间</th><th>类型 / 会话</th><th>模型</th><th>状态 / 耗时</th><th>操作</th></tr></thead><tbody>
      {page.items.map(item => <tr key={item.id}><td>#{item.id}<small>{item.create_time?.replace('T', ' ') ?? '时间未知'}</small></td><td><Tag>{types[item.type]}</Tag><small>{item.session_Id ?? '—'}</small></td><td>{item.model ?? '—'}</td><td>{statuses[item.status] ?? item.status}<small>{item.duration === null ? '耗时未知' : `${item.duration} ms`}</small></td><td><Button disabled={busy} onClick={() => void open(item.id)}>查看详情</Button></td></tr>)}
    </tbody></table>{!page.items.length && <p className="empty-list">暂无符合条件的调用记录。</p>}</div>
    <div className="catalog-pagination"><Button disabled={busy || !previous.length} onClick={() => {setCursor(previous.at(-1)); setPrevious(previous.slice(0, -1));}}>上一页</Button><Button disabled={busy || !page.next_cursor} onClick={() => {setPrevious([...previous, cursor]); setCursor(page.next_cursor!);}}>下一页</Button></div>
    {detail && <section className="panel record-detail"><div className="panel-header"><h2>调用详情 #{detail.id}</h2><Button onClick={() => {detailRequest.current?.abort(); setDetail(undefined); setSteps(undefined);}}>关闭详情</Button></div>
      <p>类型：{types[detail.type]} · 状态：{statuses[detail.status] ?? detail.status}</p>
      <p>请求 ID：{detail.request_id ?? '—'} · 用户凭据摘要：{detail.user_certificate ?? '未记录'}</p>
      <p>来源：{detail.source} · 原始 ID：{detail.source_id}</p>
      <h3>用户输入</h3><pre>{detail.content ?? '未记录'}</pre><h3>模型回复</h3><MarkdownRenderer content={detail.replay_context || '没有回复正文。'} />
      <h3>请求上下文</h3><pre>{detail.request_context === null ? '历史记录未保存请求上下文。' : JSON.stringify(detail.request_context, null, 2)}</pre>
      <h3>响应数据</h3><pre>{JSON.stringify(detail.data, null, 2) ?? '未记录'}</pre>
      {detail.type === 'agent' && detail.execution_id && <Button onClick={() => setSteps(detail.execution_id!)}>查看执行步骤与附件</Button>}
    </section>}
    {steps && <AgentRunDetail api={agent} id={steps} onClose={() => setSteps(undefined)} />}
  </div>;
}
