import { Alert, Button } from 'antd';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { errorMessage } from '../api';
import { EvolutionApi } from '../evolution-api';
import type { EvolutionKind, EvolutionRecord } from '../evolution-api';

const kinds: Record<EvolutionKind, string> = { feedback: '反馈', change: '习惯变化', reset: '重置' };
const dimensions: Record<string, string> = { verbosity: '详略', warmth: '亲切', humor: '幽默', initiative: '主动' };

/** 独立记录默认跨所有范围；筛选与分页取消旧读取，不依赖当前范围是否已有画像。 */
export function EvolutionRecords({ token, revision, disabled, onView }: {
  token: string; revision: number; disabled: boolean; onView: (profileId: number) => void;
}) {
  const api = useMemo(() => new EvolutionApi(token), [token]);
  const [kind, setKind] = useState<EvolutionKind | ''>('');
  const [scope, setScope] = useState('all');
  const [items, setItems] = useState<EvolutionRecord[]>([]);
  const [cursor, setCursor] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const pending = useRef<AbortController>();

  /** 首页替换旧筛选结果，续页仅追加更早主键；晚到的响应不能污染当前筛选。 */
  const load = useCallback(async (before = 0) => {
    pending.current?.abort();
    const controller = new AbortController(); pending.current = controller;
    setLoading(true); setError('');
    if (!before) { setItems([]); setCursor(null); }
    try {
      const page = await api.records(controller.signal, before, {
        ...(kind ? { kind } : {}), ...(scope === 'unbound' ? { unbound: true } : {}),
      });
      if (controller.signal.aborted) return;
      setItems(old => before ? [...old, ...page.items] : page.items);
      setCursor(page.next_cursor);
    } catch (failure) {
      if (!controller.signal.aborted) setError(errorMessage(failure));
    } finally { if (!controller.signal.aborted) setLoading(false); }
  }, [api, kind, scope]);

  useEffect(() => {
    void load();
    return () => pending.current?.abort();
  }, [load, revision]);

  return <section className="evolution-records" aria-label="演化记录列表" aria-busy={loading}>
    <h3>演化记录</h3>
    <p className="field-help">展示所有范围的反馈、变化和重置，可查看每次变化的依据。</p>
    <div className="record-filter evolution-record-filters">
      <label>记录类型<select value={kind} onChange={event => setKind(event.target.value as EvolutionKind | '')}>
        <option value="">全部类型</option>
        <option value="feedback">反馈</option><option value="change">习惯变化</option><option value="reset">重置</option>
      </select></label>
      <label>记录范围<select value={scope} onChange={event => setScope(event.target.value)}>
        <option value="all">全部范围</option><option value="unbound">未关联角色</option>
      </select></label>
      <Button loading={loading} disabled={disabled} onClick={() => void load()}>刷新演化记录</Button>
    </div>
    {error && <Alert type="error" showIcon message={error} />}
    {!loading && !error && !items.length && <p>暂无符合条件的演化记录。</p>}
    {items.length > 0 && <div className="table-scroll"><table className="data-table evolution-record-table">
      <thead><tr><th>时间</th><th>类型</th><th>关联角色</th><th>匿名互动对象</th><th>维度</th><th>变化</th><th>原因或反馈</th><th>详情</th></tr></thead>
      <tbody>{items.map(record => <tr key={record.id}>
        <td><time>{record.observed_at} UTC</time></td>
        <td>{kinds[record.kind]}</td>
        <td>{record.agent_id === null
          ? record.scope_key === 'global' ? '未关联角色（通用）' : '未关联角色'
          : record.agent_name}</td>
        <td>互动对象 {record.subject_key.slice(0, 8)}</td>
        <td>{record.dimension ? dimensions[record.dimension] : '全部维度'}</td>
        <td>{record.before_value !== null && record.after_value !== null
          ? `${record.before_value} → ${record.after_value}` : '—'}</td>
        <td>{record.excerpt}</td>
        <td><Button disabled={disabled} onClick={() => onView(record.profile_id)}>查看依据</Button></td>
      </tr>)}</tbody>
    </table></div>}
    {cursor !== null && <Button loading={loading} disabled={disabled}
      onClick={() => void load(cursor)}>加载更早演化记录</Button>}
  </section>;
}
