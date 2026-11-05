import { Alert, Button, Empty, Input, Space, Switch, Tag, Tooltip } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { useCallback, useEffect, useRef, useState } from 'react';
import type { AutonomyApi, ConversationEntry, Profile } from '../autonomy-api';
import { participant } from '../autonomy-api';
import { errorMessage } from '../api';
import { MarkdownRenderer } from './MarkdownRenderer';

const actionNames: Record<string, string> = { plan: '生成计划', recall: '长期检索', draft: '文字整理', consolidate: '记忆整理', review: '复盘', report: '汇报' };
const statusNames: Record<string, string> = { pending: '待执行', running: '执行中', retry: '等待重试', blocked: '策略等待', approval: '待审批', succeeded: '已完成', failed: '失败', cancelled: '已取消' };

/** 显式格式化UTC时间，来源发生时间与任务创建时间分别显示。 */
function time(value: string) {
  return new Date(value).toLocaleString('zh-CN', { hour12: false });
}

/** 结果只作安全文本或Markdown渲染，汇报回执保持入队与实际投递的区别。 */
function Result({ value }: { value: unknown }) {
  if (typeof value === 'string') return <MarkdownRenderer content={value} />;
  if (value === null || value === undefined) return <span>步骤已完成</span>;
  if (typeof value === 'object') {
    const result = value as Record<string, unknown>;
    if (result.suppressed) return <Tag>重复汇报已抑制</Tag>;
    if (typeof result.text === 'string') return <>
      {'queued' in result && <Tag color={result.delivery === 'delivered' ? 'green' : 'default'}>{result.delivery === 'delivered' ? '已投递' : result.queued ? '待投递' : '本地汇报'}</Tag>}
      <MarkdownRenderer content={result.text} />
    </>;
    if (typeof result.summary === 'string') return <MarkdownRenderer content={result.summary} />;
    if (Array.isArray(result.records)) return result.records.length ? <ul className="autonomy-recalled">
      {result.records.map((record, index) => <li key={index}>{String((record as Record<string, unknown>).content || '')}</li>)}
    </ul> : <span>未检索到记忆</span>;
    if (typeof result.open_tasks === 'number') return <p>未完成任务：{result.open_tasks} · 已完成任务：{String(result.completed_tasks ?? 0)}</p>;
  }
  return <details><summary>结果详情</summary><pre className="autonomy-result">{JSON.stringify(value, null, 2)}</pre></details>;
}

/** 循环对话只读持久结果；切换身份取消旧请求，定时刷新不执行任务或调用模型。 */
export function AutonomyConversation({ api, profile, active }: { api: AutonomyApi; profile: Profile; active: boolean }) {
  const [entries, setEntries] = useState<ConversationEntry[]>([]);
  const [selected, setSelected] = useState('');
  const [before, setBefore] = useState<string | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(profile.owner.channel === 'local');
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const requestKey = useRef<string>();
  const pending = useRef<AbortController>();
  const entry = entries.find(item => item.id === selected);

  /** 刷新最新一页并重新核对来源期限；翻页追加由服务端提供的稳定游标。 */
  const load = useCallback(async (cursor = '') => {
    if (pending.current) return;
    const controller = new AbortController(); pending.current = controller;
    setLoading(true); setError('');
    try {
      const page = await api.conversations(profile.id, cursor, controller.signal);
      if (controller.signal.aborted) return;
      if (cursor) {
        setEntries(old => [...old.filter(item => !page.items.some(next => next.id === item.id)), ...page.items]);
      } else {
        // 刷新目录后丢弃旧页，避免将到期的来源正文留在历史选项中。
        setEntries(page.items);
        setSelected(old => page.items.some(item => item.id === old) ? old : page.items[0]?.id || '');
      }
      setBefore(page.next_before);
    } catch (failure) {
      if (!controller.signal.aborted) { setError(errorMessage(failure)); setEntries([]); setSelected(''); }
    } finally {
      if (pending.current === controller) { pending.current = undefined; setLoading(false); }
    }
  }, [api, profile.id]);

  useEffect(() => {
    if (active) void load();
    return () => { pending.current?.abort(); pending.current = undefined; };
  }, [load, active]);
  useEffect(() => {
    if (!active || !refreshing) return;
    const timer = window.setInterval(() => { if (!document.hidden) void load(); }, 5000);
    return () => window.clearInterval(timer);
  }, [active, refreshing, load]);

  /** 网络失败保留正文和幂等键，重试不会创建重复回复任务。 */
  async function send() {
    if (sending || !draft.trim()) return;
    setSending(true); setError('');
    requestKey.current ||= crypto.randomUUID();
    try {
      await api.send(profile.id, draft.trim(), requestKey.current);
      setDraft(''); requestKey.current = undefined; setSelected('');
      pending.current?.abort(); pending.current = undefined;
      await load(); setRefreshing(true);
    } catch (failure) { setError(errorMessage(failure)); }
    finally { setSending(false); }
  }

  return <section className="autonomy-conversations" aria-label="自主循环对话">
    <div className="autonomy-conversation-toolbar"><h2>循环对话</h2><Space wrap>
      <label>自动刷新 <Switch aria-label="自动刷新对话" checked={refreshing} onChange={setRefreshing} /></label>
      <Tooltip title="刷新对话"><Button icon={<ReloadOutlined />} aria-label="刷新对话" loading={loading} onClick={() => void load()} /></Tooltip>
    </Space></div>
    {error && <Alert type="error" showIcon message={error} />}
    {!entries.length ? <Empty description={loading ? '正在读取对话' : '暂无循环对话'} /> : <div className="autonomy-conversation-layout">
      <nav className="autonomy-conversation-list" aria-label="对话记录">
        {entries.map(item => <button key={item.id} type="button" className={item.id === selected ? 'selected' : ''} aria-pressed={item.id === selected} onClick={() => setSelected(item.id)}>
          <span className="autonomy-conversation-title">{item.task?.goal || item.episode?.text || '来源已过期'}</span>
          <span className="autonomy-conversation-meta"><time>{time(item.created_at)}</time><span>{item.task ? statusNames[item.task.status] || item.task.status : '来源发言'}</span></span>
        </button>)}
        {before && <Button block disabled={loading} onClick={() => void load(before)}>更早对话</Button>}
      </nav>
      {entry && <div className="autonomy-conversation-thread" aria-label="对话内容" aria-busy={loading}>
        <header><strong>{profile.identity.name}</strong><Tag>{entry.task ? statusNames[entry.task.status] || entry.task.status : '来源发言'}</Tag><time>{time(entry.created_at)}</time></header>
        {entry.source_expired && <Alert type="info" message="来源情节已过期" showIcon />}
        {entry.episode && <article className="autonomy-message source"><div className="autonomy-message-author">{entry.episode.source === 'self_report' ? `用户 ${participant(profile.owner)}` : '第三方资料'}<time>{time(entry.episode.occurred_at)}</time></div><p>{entry.episode.text}</p></article>}
        {entry.task && <>
          <article className="autonomy-message goal"><div className="autonomy-message-author">任务目标</div><p>{entry.task.goal}</p></article>
          {entry.task.steps.map((step, index) => <article className="autonomy-message assistant" key={index}>
            <div className="autonomy-message-author">{profile.identity.name}<Tag>{actionNames[step.action]}</Tag><span>步骤 {index + 1}</span>
              <Tag>{index < entry.task!.outputs.length ? '已完成' : index === entry.task!.cursor ? statusNames[entry.task!.status] || entry.task!.status : '未执行'}</Tag></div>
            {step.text && <p>{step.text}</p>}
            {index < entry.task!.outputs.length && <Result value={entry.task!.outputs[index]} />}
          </article>)}
          {entry.task.error_code && <Alert type={entry.task.status === 'failed' ? 'error' : 'warning'} showIcon message={entry.task.error_code} />}
        </>}
        {!entry.task && entry.episode?.summary && <article className="autonomy-message assistant"><div className="autonomy-message-author">{profile.identity.name}<Tag>情节摘要</Tag></div><MarkdownRenderer content={entry.episode.summary} /></article>}
      </div>}
    </div>}
    {profile.owner.channel === 'local' && <form className="autonomy-composer" onSubmit={event => { event.preventDefault(); void send(); }}>
      <label htmlFor="autonomy-message">与 {profile.identity.name} 对话</label>
      <Input.TextArea id="autonomy-message" aria-label="发送给独立智能体" value={draft} maxLength={4000} rows={3} disabled={sending}
        onChange={event => { setDraft(event.target.value); requestKey.current = undefined; }} placeholder="输入消息，回复由独立循环处理…" />
      <div><span>{profile.policy.paused ? '身份已暂停，消息保存后等待恢复' : '消息持久化后由独立循环回复'}</span><Button type="primary" htmlType="submit" loading={sending} disabled={!draft.trim()}>发送消息</Button></div>
    </form>}
  </section>;
}
