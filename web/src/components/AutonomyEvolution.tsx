import { Alert, Button, Empty, Table } from 'antd';
import { useRef, useEffect, useState } from 'react';
import type { AutonomyApi } from '../autonomy-api';
import type { EvolutionResearch } from '../evolution-api';
import { errorMessage } from '../api';

const labels = { verbosity: '详略', warmth: '亲切', humor: '幽默', initiative: '主动' };

/** 独立主体的研究与应用入口；未预览或证据冲突时不允许直接写习惯。 */
export function AutonomyEvolution({ api, id }: { api: AutonomyApi; id: string }) {
  const [research, setResearch] = useState<EvolutionResearch | null>();
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  const pending = useRef<AbortController>();
  useEffect(() => () => pending.current?.abort(), []);
  /** 当前研究只读取本人真实反馈，不调用模型或消费证据。 */
  async function preview() {
    const controller = new AbortController(); pending.current = controller;
    setBusy(true); setError('');
    try { const result = await api.evolution(id, controller.signal); if (!controller.signal.aborted) setResearch(result.research); }
    catch (failure) { if (!controller.signal.aborted) setError(errorMessage(failure)); }
    finally { if (!controller.signal.aborted) setBusy(false); }
  }
  /** 手动应用重新核对反馈与版本，失败后保留预览便于重新研究。 */
  async function apply() {
    if (!research) return;
    setBusy(true); setError('');
    try { await api.advanceEvolution(id, research); await preview(); }
    catch (failure) { setError(errorMessage(failure)); }
    finally { setBusy(false); }
  }
  return <section aria-label="独立Agent自我演进">
    <p>研究自己的真实互动反馈，预览表达习惯变化后再手动演进。</p>
    <Button onClick={() => void preview()} loading={busy}>预览演进研究</Button>
    {error && <Alert type="error" message={error} />}
    {research === null && <Empty description="尚未积累明确的长期反馈，可在对话中说明“以后请简洁一点”等偏好。" />}
    {research && <><Table pagination={false} rowKey="dimension" dataSource={research.changes} columns={[
      { title: '习惯', dataIndex: 'dimension', render: (value: keyof typeof labels) => labels[value] },
      { title: '预计变化', render: (_, row) => `${row.before} → ${row.after}` },
      { title: '净支持', dataIndex: 'net_support' }, { title: '不同日期', dataIndex: 'dates' },
    ]} /><p>沿用最近90天内至少3个日期、跨度7天的真实反馈，不生成或伪造依据。</p>
      <Button type="primary" loading={busy} disabled={!research.changes.some(row => row.eligible && row.before !== row.after)} onClick={() => void apply()}>手动演进</Button></>}
  </section>;
}
