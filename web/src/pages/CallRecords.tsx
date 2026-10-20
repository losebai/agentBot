import { ChoiceSelect } from '../components/ChoiceSelect';
import { Alert, Button, Tag } from 'antd';
import { useEffect, useMemo, useRef, useState } from 'react';
import { AgentApi } from '../agent-api';
import { errorMessage } from '../api';
import { CallRecordsApi } from '../call-records-api';
import type { CallCategory, CallDetail, CallFilters, CallSummary } from '../call-records-api';
import type { Page, TokenUsage } from '../ai-types';
import { AgentRunDetail } from '../components/AgentRunDetail';
import { MarkdownRenderer } from '../components/MarkdownRenderer';
import { DetailDialog } from '../components/DetailDialog';
import { AgentToolSteps } from '../components/AgentToolSteps';

const types = {ordinary: '普通调用', agent: 'Agent 调用', auto_speak: '自动发言决策', unknown: '历史未知类型'};
const systemTypes = {auto_speak: '自动发言决策', command: '指令调用'};
const statuses: Record<string, string> = {running: '运行中', completed: '已完成', failed: '失败', cancelled: '已取消', unknown: '未知'};

/** 用量缺失显示未知，明确零值仍显示 0，数字不做缩写以便核对账单。 */
function tokenCount(value: number | null | undefined): string {
  return value == null ? '—' : value.toLocaleString('zh-CN');
}

/** 列表与详情共用用量展示；多轮 Agent 只对上报缓存的输入计算命中率。 */
function CallTokenUsage({ usage, compact = false }: { usage?: TokenUsage; compact?: boolean }) {
  const denominator = usage?.cache_prompt_tokens ?? usage?.prompt_tokens;
  const hitRate = usage?.cached_tokens != null && denominator != null && denominator > 0
    ? `${(usage.cached_tokens / denominator * 100).toFixed(1)}%` : '—';
  const metrics = [
    ['输入', tokenCount(usage?.prompt_tokens)],
    ['输出', tokenCount(usage?.completion_tokens)],
    ['缓存', tokenCount(usage?.cached_tokens)],
    ['总计', tokenCount(usage?.total_tokens)],
    ['缓存命中率', hitRate],
  ];
  return <div className={`call-token-usage${compact ? ' compact' : ''}`}>
    <dl>{metrics.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
    {!compact && <p>缓存 Token 已包含在输入中，不额外计入总量。未上报的用量显示为 —。
      {usage?.cached_tokens != null && denominator != null
        && <> 缓存统计覆盖的输入：{tokenCount(denominator)} Token。</>}
    </p>}
  </div>;
}

/** 普通和系统调用复用列表、用量与详情，分组筛选在后端分页前完成。 */
export function CallRecords({token, category = 'conversation'}: {token: string; category?: CallCategory}) {
  const system = category === 'system';
  const initial: CallFilters = {type: '', session_id: '', status: '', system_kind: ''};
  const api = useMemo(() => new CallRecordsApi(token), [token]);
  const agent = useMemo(() => new AgentApi(token), [token]);
  const [draft, setDraft] = useState(initial), [filters, setFilters] = useState(initial);
  const [page, setPage] = useState<Page<CallSummary>>({items: [], next_cursor: null});
  const [cursor, setCursor] = useState<string>(), [previous, setPrevious] = useState<(string | undefined)[]>([]);
  const [detail, setDetail] = useState<CallDetail>(), [steps, setSteps] = useState<string>();
  const [error, setError] = useState(''), [busy, setBusy] = useState(false), [refresh, setRefresh] = useState(0);
  const detailRequest = useRef<AbortController>();
  const [selected, setSelected] = useState<number>();
  const [detailLoading, setDetailLoading] = useState(false), [detailError, setDetailError] = useState('');

  useEffect(() => {
    const controller = new AbortController();
    detailRequest.current?.abort(); setSelected(undefined); setDetail(undefined); setSteps(undefined); setBusy(true);
    // 切换筛选时丢弃迟到的列表响应，避免页码与内容不一致。
    api.list({...filters, category}, cursor, controller.signal).then(result => {
      if (!controller.signal.aborted) { setPage(result); setError(''); }
    }).catch(e => { if (!controller.signal.aborted) setError(errorMessage(e)); })
      .finally(() => { if (!controller.signal.aborted) setBusy(false); });
    return () => { controller.abort(); detailRequest.current?.abort(); };
  }, [api, filters, cursor, refresh, category]);

  /** 关闭时立即取消请求，迟到的响应不能重新打开详情或覆盖列表。 */
  function close() {
    detailRequest.current?.abort(); setSelected(undefined); setDetail(undefined); setSteps(undefined);
  }

  /** 先打开弹窗再加载正文，错误与加载状态都留在当前详情内。 */
  async function open(id: number) {
    detailRequest.current?.abort();
    const controller = new AbortController(); detailRequest.current = controller;
    setSelected(id); setDetail(undefined); setSteps(undefined); setDetailLoading(true); setDetailError('');
    try {
      const result = await api.detail(id, controller.signal);
      if (!controller.signal.aborted) setDetail(result);
    } catch (e) { if (!controller.signal.aborted) setDetailError(errorMessage(e)); }
    finally { if (!controller.signal.aborted) setDetailLoading(false); }
  }
  return <div className="page-content">
    <div className="page-heading"><div><p className="eyebrow">{system ? 'SYSTEM CALLS' : 'CALL HISTORY'}</p><h1>{system ? '系统调用' : '调用记录'}</h1><p>{system ? '查看自动发言决策和机器人显式指令触发的调用。' : '查看普通对话、Agent 对话和迁入的历史记录。'}</p></div><Button loading={busy} onClick={() => setRefresh(value => value + 1)}>刷新记录</Button></div>
    {error && <Alert className="notice" type="error" message={error} />}
    <form className="record-filter" onSubmit={e => { e.preventDefault(); setPrevious([]); setCursor(undefined); setFilters({...draft}); }}>
      <label>类型 <ChoiceSelect aria-label="类型" value={system ? draft.system_kind : draft.type} onChange={e => setDraft({...draft, [system ? 'system_kind' : 'type']: e})}><option value="">全部类型</option>{Object.entries(system ? systemTypes : types).filter(([key]) => system || key !== 'auto_speak').map(([key, name]) => <option key={key} value={key}>{name}</option>)}</ChoiceSelect></label>
      <label>会话 ID <input value={draft.session_id} maxLength={255} onChange={e => setDraft({...draft, session_id: e.target.value})} /></label>
      <label>状态 <ChoiceSelect aria-label="状态" value={draft.status} onChange={e => setDraft({...draft, status: e})}><option value="">全部状态</option>{Object.entries(statuses).map(([key, name]) => <option key={key} value={key}>{name}</option>)}</ChoiceSelect></label>
      <Button htmlType="submit" disabled={busy}>筛选</Button>
    </form>
    <div className="panel table-wrap"><table className="data-table"><thead><tr><th>ID / 时间</th><th>类型 / 会话</th><th>模型</th><th>Token 用量</th><th>状态 / 耗时</th><th>操作</th></tr></thead><tbody>
      {page.items.map(item => <tr key={item.id}><td>#{item.id}<small>{item.create_time?.replace('T', ' ') ?? '时间未知'}</small></td><td><Tag>{item.system_kind ? systemTypes[item.system_kind] : types[item.type]}</Tag><small>{item.session_Id ?? '—'}</small></td><td>{item.model ?? '—'}</td><td><CallTokenUsage usage={item.usage} compact /></td><td>{statuses[item.status] ?? item.status}<small>{item.duration === null ? '耗时未知' : `${item.duration} ms`}</small></td><td><Button disabled={busy} onClick={() => void open(item.id)}>查看详情</Button></td></tr>)}
    </tbody></table>{!page.items.length && <p className="empty-list">暂无符合条件的调用记录。</p>}</div>
    <div className="catalog-pagination"><Button disabled={busy || !previous.length} onClick={() => {setCursor(previous.at(-1)); setPrevious(previous.slice(0, -1));}}>上一页</Button><Button disabled={busy || !page.next_cursor} onClick={() => {setPrevious([...previous, cursor]); setCursor(page.next_cursor!);}}>下一页</Button></div>
    {selected !== undefined && <DetailDialog title={`调用详情 #${selected}`} loading={detailLoading} error={detailError} onClose={close}>
      {detail && <>
      <p>类型：{detail.system_kind ? systemTypes[detail.system_kind] : types[detail.type]} · 状态：{statuses[detail.status] ?? detail.status}</p>
      <p>请求 ID：{detail.request_id ?? '—'} · 用户凭据摘要：{detail.user_certificate ?? '未记录'}</p>
      <p>来源：{detail.source} · 原始 ID：{detail.source_id}</p>
      <h3>Token 用量</h3><CallTokenUsage usage={detail.usage} />
      <h3>用户输入</h3><pre>{detail.content ?? '未记录'}</pre><h3>模型回复</h3><MarkdownRenderer content={detail.replay_context || '没有回复正文。'} />
      <h3>请求上下文</h3><pre>{detail.request_context === null ? '历史记录未保存请求上下文。' : JSON.stringify(detail.request_context, null, 2)}</pre>
      <h3>响应数据</h3><pre>{JSON.stringify(detail.data, null, 2) ?? '未记录'}</pre>
      {detail.type === 'agent' && <><h3>工具调用</h3><AgentToolSteps steps={detail.tool_steps ?? []} /></>}
      {detail.type === 'agent' && detail.execution_id && <Button onClick={() => setSteps(detail.execution_id!)}>查看执行步骤与附件</Button>}
      </>}
    </DetailDialog>}
    {steps && <AgentRunDetail api={agent} id={steps} onClose={() => setSteps(undefined)} />}
  </div>;
}
