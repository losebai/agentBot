import { Alert, Button } from 'antd';
import { useEffect, useMemo, useRef, useState } from 'react';
import { errorMessage } from '../api';
import { EvolutionApi } from '../evolution-api';
import type { EvolutionConfig, EvolutionHistory, EvolutionProfile } from '../evolution-api';
import { EvolutionRecords } from './EvolutionRecords';

const labels: Record<string, string> = {
  verbosity: '详略', warmth: '亲切', humor: '幽默', initiative: '主动',
};

/** 匿名对象的档位只表示长期表达习惯，不是能力或情绪评分。 */
function traitText(profile: EvolutionProfile): string {
  return Object.entries(profile.traits).map(([key, value]) => `${labels[key]} ${value > 0 ? '+' : ''}${value}`).join(' · ');
}

/** 通用演化无需角色；切换范围重新挂载，取消旧查询与旧写入响应的展示。 */
export function AgentEvolution({ token, agentId = null }: { token: string; agentId?: number | null }) {
  return <section className="panel agent-evolution" aria-label="自我演进管理">
    <EvolutionBody key={`${agentId}-${token}`} token={token} agentId={agentId} />
  </section>;
}

/** 暂停、重置独立于角色草稿立即保存；冲突时保留当前内容并提示刷新。 */
function EvolutionBody({ token, agentId }: { token: string; agentId: number | null }) {
  const api = useMemo(() => new EvolutionApi(token), [token]);
  const [config, setConfig] = useState<EvolutionConfig>();
  const [profiles, setProfiles] = useState<EvolutionProfile[]>([]);
  const [cursor, setCursor] = useState<number | null>(null);
  const [history, setHistory] = useState<EvolutionHistory>();
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [recordRevision, setRecordRevision] = useState(0);
  const alive = useRef(true);
  const pending = useRef<AbortController>();
  const reading = useRef<AbortController>();
  const detail = useRef<HTMLElement>(null);

  /** 刷新目录不保留过期画像版本；已有分页可明确继续加载。 */
  async function load(after = 0) {
    pending.current?.abort(); reading.current?.abort();
    const controller = new AbortController();
    pending.current = controller;
    setLoading(true); setError(''); setHistory(undefined); setConfirm(false);
    try {
      const page = await api.profiles(agentId, controller.signal, after);
      if (controller.signal.aborted) return;
      setConfig(page.config); setCursor(page.next_cursor);
      setProfiles(old => after ? [...old, ...page.items] : page.items);
    } catch (failure) {
      if (!controller.signal.aborted) setError(errorMessage(failure));
    } finally { if (!controller.signal.aborted) setLoading(false); }
  }

  useEffect(() => {
    alive.current = true;
    void load();
    return () => { alive.current = false; pending.current?.abort(); reading.current?.abort(); };
    // 当前组件按范围与 token 重新挂载，保证每一代查询只写入自己的状态。
  }, [api, agentId]);

  useEffect(() => {
    // 详情位于长列表之后，读取成功后移入焦点，使键盘和窄屏用户能立即找到依据。
    if (history) detail.current?.focus();
  }, [history?.profile.id]);

  /** 切换对象即取消旧历史，晚到响应不得污染新选择。 */
  async function read(profileId: number, before = 0) {
    reading.current?.abort();
    const controller = new AbortController(); reading.current = controller;
    setError(''); setConfirm(false);
    if (!before) setHistory(undefined);
    try {
      const page = await api.history(profileId, controller.signal, before);
      if (controller.signal.aborted) return;
      setHistory(old => before && old?.profile.id === profileId
        ? { ...page, items: [...old.items, ...page.items] } : page);
    } catch (failure) {
      if (!controller.signal.aborted) setError(errorMessage(failure));
    }
  }

  /** 服务端成功后才更新开关，避免请求失败仍看似暂停。 */
  async function toggle() {
    if (!config || busy) return;
    setBusy(true); setError(''); setNotice('');
    try {
      const value = await api.configure(agentId, config);
      if (alive.current) { setConfig(value); setNotice(value.enabled ? '已恢复长期互动演化。' : '已暂停学习和习惯应用。'); }
    } catch (failure) { if (alive.current) setError(errorMessage(failure)); }
    finally { if (alive.current) setBusy(false); }
  }

  /** 使用所见历史的最新画像版本重置，失败保留证据与确认状态。 */
  async function reset() {
    if (!history || busy) return;
    setBusy(true); setError(''); setNotice('');
    try {
      const profile = await api.reset(history.profile);
      if (!alive.current) return;
      setProfiles(old => old.map(item => item.id === profile.id ? profile : item));
      setNotice('习惯已重置，旧反馈不再参与演化。');
      setRecordRevision(old => old + 1);
      await read(profile.id);
    } catch (failure) { if (alive.current) setError(errorMessage(failure)); }
    finally { if (alive.current) setBusy(false); }
  }

  return <div className="agent-evolution-body">
    <p>长期互动逐步形成表达习惯。相同对象的明确反馈需出现在至少 3 个不同日期，跨度至少 7 天；相反反馈会抵消，每次只调整一档。</p>
    <p className="field-help">先支持“以后请简洁一点”“以后请详细一点”“以后请亲切一点”“以后请幽默一点”“以后请主动一点”等完整反馈句。使用最近 90 天的证据，个人习惯互相隔离；不修改角色和工具权限。此处操作立即保存。</p>
    <p className="field-help">通用习惯适用于所有普通对话；角色范围的非零习惯优先，零值沿用通用习惯。各范围分别暂停和重置。</p>
    <p className="field-help">档位范围 -3 至 +3。负值趋向简洁、正式、严肃、减少主动建议；正值趋向详细、亲切、幽默、增加主动建议。</p>
    {error && <Alert type="error" showIcon message={error} />}
    {notice && <Alert type="success" showIcon message={notice} />}
    <div className="heading-actions">
      {config && <label className="checkbox-label"><input type="checkbox" checked={config.enabled}
        disabled={busy || loading} onChange={() => void toggle()} />启用长期互动演化</label>}
      <Button loading={loading} disabled={busy} onClick={() => void load()}>刷新演化状态</Button>
    </div>
    {!loading && !profiles.length && <p>尚未积累明确反馈。</p>}
    {profiles.map(profile => <div key={profile.id} className="panel agent-evolution-profile">
      <p>互动对象 {profile.subject_key.slice(0, 8)}</p><p>{traitText(profile)}</p>
      <Button disabled={busy || loading} onClick={() => void read(profile.id)}>查看演化记录</Button>
    </div>)}
    {cursor !== null && <Button disabled={busy || loading} onClick={() => void load(cursor)}>加载更多对象</Button>}
    <EvolutionRecords token={token} revision={recordRevision} disabled={busy}
      onView={profileId => void read(profileId)} />
    {history && <section ref={detail} tabIndex={-1} className="agent-evolution-history" aria-label="演化记录">
      <h3>互动对象 {history.profile.subject_key.slice(0, 8)} 的变化记录</h3>
      <p>{history.profile.agent_id === null
        ? history.profile.scope_key === 'global' ? '未关联角色（通用）' : '未关联角色'
        : `关联角色 #${history.profile.agent_id}`}</p>
      <p>{traitText(history.profile)}</p>
      <Button danger disabled={busy || loading} onClick={() => setConfirm(true)}>重置此对象的习惯</Button>
      {confirm && <div role="group" aria-label="重置习惯确认">
        <p>重置后恢复原始表达习惯，旧反馈不再参与演化，变化记录保留。</p>
        <Button danger disabled={busy} onClick={() => void reset()}>确认重置</Button>
        <Button disabled={busy} onClick={() => setConfirm(false)}>取消重置</Button>
      </div>}
      <ol>{history.items.map(event => <li key={event.id}>
        <p>#{event.id} · {event.observed_at} UTC · {event.kind === 'feedback' ? '反馈' : event.kind === 'change' ? '习惯变化' : '重置'}
          {event.dimension && ` · ${labels[event.dimension]}`}
          {event.kind === 'change' && ` ${event.before_value} → ${event.after_value}`}</p>
        <p>{event.excerpt}</p>
        {event.evidence_ids.length > 0 && <p>依据反馈 {event.evidence_ids.map(id => `#${id}`).join('、')}</p>}
      </li>)}</ol>
      {history.next_cursor !== null && <Button disabled={busy || loading}
        onClick={() => void read(history.profile.id, history.next_cursor!)}>加载更早记录</Button>}
    </section>}
  </div>;
}
