import { Alert, Button, Input, Spin, Tag } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { useEffect, useMemo, useRef, useState } from 'react';
import { ApiClient, errorMessage } from '../api';
import type { BotInfo, NapCatStatus as NapCatObservation, ServiceStatus } from '../types';
import { NapCatStatus } from '../components/NapCatStatus';

/** 归档按北京时间计日，浏览器所在时区不能改变今日统计的边界。 */
function todayRange() {
  const today = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai' }).format(new Date());
  return { start_date: today, end_date: today };
}

/** 展示所选日期的归档用量、模型失败率及实时服务状态。 */
export function Overview({ token }: { token: string }) {
  const api = useMemo(() => new ApiClient(token), [token]);
  const [status, setStatus] = useState<ServiceStatus>();
  const [bots, setBots] = useState<BotInfo[]>([]);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [napcat, setNapcat] = useState<NapCatObservation>();
  const [napcatError, setNapcatError] = useState('');
  const [range, setRange] = useState(todayRange);
  const [draft, setDraft] = useState(range);
  const validRange = Boolean(draft.start_date && draft.end_date && draft.start_date <= draft.end_date);
  const pending = useRef<AbortController | null>(null);
  /** 各观察接口独立失败；取消和身份检查一起阻止旧读取覆盖新状态。 */
  async function refresh() {
    pending.current?.abort();
    const controller = new AbortController();
    pending.current = controller;
    setLoading(true); setError('');
    // 切换条件或刷新时清除旧指标，失败后不能把上一日期的统计当作当前结果。
    setStatus(previous => previous && { ...previous, statistics: null, statistics_error: null });
    const [next, connections, managed] = await Promise.allSettled([
      api.status(controller.signal, range), api.bots(controller.signal), api.napcatStatus(controller.signal),
    ]);
    if (controller.signal.aborted || pending.current !== controller) return;
    if (next.status === 'fulfilled') setStatus(next.value);
    else setError(errorMessage(next.reason));
    if (connections.status === 'fulfilled') setBots(connections.value);
    else setError(errorMessage(connections.reason));
    if (managed.status === 'fulfilled') { setNapcat(managed.value); setNapcatError(''); }
    else setNapcatError(errorMessage(managed.reason));
    setLoading(false);
  }
  useEffect(() => { void refresh(); return () => pending.current?.abort(); }, [api, range]);
  const statistics = status?.statistics;
  // 统计故障独立显示，归档尚未升级时仍可查看机器人连接和服务就绪状态。
  const statisticsError = status?.statistics_error === 'AI_NOT_INITIALIZED'
    ? '用量统计不可用：调用记录尚未初始化，请完成数据库初始化或升级。'
    : status?.statistics_error ? '用量统计暂不可用，请检查数据库连接后刷新。' : '';
  /** 未就绪的统计显示未知，已知计数使用分组数字便于比较。 */
  function count(value: number | undefined) {
    return value === undefined ? '—' : value.toLocaleString('zh-CN');
  }
  return <div className="page-content">
    <div className="page-heading"><div><p className="eyebrow">WORKSPACE OVERVIEW</p><h1>运行概览</h1><p>查看对话活动、模型用量与服务状态。</p></div><Button icon={<ReloadOutlined />} loading={loading} onClick={() => void refresh()}>刷新状态</Button></div>
    <form className="overview-filters" aria-label="统计时间筛选" onSubmit={event => {
      event.preventDefault();
      if (validRange) setRange({ ...draft });
    }}>
      <label>开始日期<Input type="date" value={draft.start_date} onChange={event => setDraft({ ...draft, start_date: event.target.value })} /></label>
      <label>结束日期<Input type="date" value={draft.end_date} onChange={event => setDraft({ ...draft, end_date: event.target.value })} /></label>
      <Button type="primary" htmlType="submit" disabled={!validRange}>查询</Button>
      <Button onClick={() => { const today = todayRange(); setDraft(today); setRange(today); }}>今日</Button>
      <span>北京时间 · 含起止日期</span>
      {!validRange && <span role="alert">请选择完整日期，且开始日期不能晚于结束日期。</span>}
    </form>
    {error && <Alert message={error} type="error" showIcon className="notice" />}
    {statisticsError && <Alert message={statisticsError} type="warning" showIcon className="notice" />}
    {loading && !status ? <div className="loading"><Spin /></div> : status && <>
      <div className="metrics-grid overview-metrics" aria-label="对话与用量统计" aria-busy={loading}>
        <div className="metric"><span>对话总数</span><strong>{count(statistics?.conversation_count)}</strong><p>已归档的普通 / Agent 对话</p></div>
        <div className="metric"><span>当前运行中</span><strong>{count(statistics?.running_requests)}</strong><p>当前执行中的对话 / Agent 请求</p></div>
        <div className="metric"><span>Token 消耗</span><strong>{count(statistics?.total_tokens)}</strong><p>所选日期内的用量，包含系统调用</p></div>
        <div className="metric"><span>缓存命中率</span><strong>{statistics?.cache_hit_rate == null ? '—' : `${(statistics.cache_hit_rate * 100).toFixed(1)}%`}</strong><p>{statistics?.cache_hit_rate == null ? '暂无缓存用量' : `缓存输入 ${count(statistics.cached_tokens)} / ${count(statistics.cache_prompt_tokens)}`}</p></div>
        <div className="metric"><span>模型调用失败率</span><strong>{statistics?.model_failure_rate == null ? '—' : `${(statistics.model_failure_rate * 100).toFixed(1)}%`}</strong><p>{!statistics ? '统计不可用' : statistics.model_failure_rate == null ? '暂无已结束的模型调用' : `失败 ${count(statistics.failed_model_calls)} / 已结束 ${count(statistics.model_call_count)}`}</p><p>仅计成功或失败的模型调用</p></div>
      </div>
      <div className="metrics-grid">
        <div className="metric"><span>服务状态</span><strong className={status.ready ? 'healthy' : 'waiting'}>{status.ready ? '就绪' : '依赖异常'}</strong><p>数据库与持久队列</p></div>
        <div className="metric"><span>在线机器人</span><strong>{status.bot_count}<small> 个</small></strong><p>当前 OneBot 连接</p></div>
        <div className="metric"><span>保存版本</span><strong>{status.database_revision}</strong><p>数据库配置</p></div>
        <div className="metric"><span>应用版本</span><strong>{status.applied_revision}</strong><p>当前运行时</p></div>
      </div>
      {status.restart_required_fields.length > 0 && <Alert className="notice" showIcon type="warning" message={`以下设置需要重启：${status.restart_required_fields.join(', ')}`} />}
      <section className="panel capability-summary"><strong>AI 能力</strong><Tag>普通对话</Tag><Tag>Agent</Tag><Tag>媒体</Tag>{status.python_media&&<span>媒体存储：{status.python_media.code||(status.python_media.storage_available?'已就绪':'未就绪')}</span>}{status.python_ai && <span>Python 配置：{status.python_ai.code || (status.python_ai.initialized?'已初始化':'未初始化')} · 模型 {status.configured_model}</span>}{status.python_agent && <span>Agent 目录：{status.python_agent.code || (status.python_agent.initialized?'已初始化':'未初始化')} · 预设 {status.configured_agent || '使用聊天配置'}</span>}<Tag color={status.job_enabled?'green':'default'}>Job：{status.job_enabled?'已启用':'关闭'}</Tag><Tag color={status.auto_speak_enabled?'green':'default'}>自动发言：{status.auto_speak_enabled?'已启用':'关闭'}</Tag></section>
      <NapCatStatus status={napcat} botCount={bots.length} error={napcatError} />
      <div className="overview-grid"><section className="panel"><div className="panel-header"><h2>机器人连接</h2><Tag>{bots.length} 个在线</Tag></div>
        {!bots.length ? <div className="empty-connections"><div className="connection-symbol">○</div><h3>等待机器人连接</h3><p>在 NapCat 中配置反向 WebSocket，连接此服务的<br /><code>/onebot/v11/ws</code>。</p></div>
          : bots.map(bot => <div className="bot-row" key={bot.id}><div className="bot-avatar">AB</div><div><strong>{bot.id}</strong><p>{bot.adapter}</p></div><Tag color="green">在线</Tag></div>)}</section>
        <section className="panel"><div className="panel-header"><h2>工作台说明</h2></div><div className="info-list"><div><span className="info-dot" /><div><h3>配置集中管理</h3><p>修改后查看保存与应用版本，确认是否需要重启。</p></div></div><div><span className="info-dot" /><div><h3>对话独立测试</h3><p>先验证模型回复，再接入 QQ 群聊或私聊。</p></div></div><div><span className="info-dot" /><div><h3>持续运行</h3><p>关闭浏览器不会停止机器人服务。</p></div></div></div></section></div>
    </>}
  </div>;
}
