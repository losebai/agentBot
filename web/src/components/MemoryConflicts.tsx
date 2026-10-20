import { Alert, Button } from 'antd';
import { useEffect, useRef, useState } from 'react';
import { ApiFailure, errorMessage } from '../api';
import type { KnowledgeApi } from '../knowledge-api';
import type { MemoryEntry } from '../knowledge-types';
import { MemoryFacts } from './MemoryFacts';

/** 整组核对冲突候选；仅在明确选择和说明原因后提交全部版本。 */
export function MemoryConflicts({api, id, onResolved}: {api: KnowledgeApi; id: number; onResolved: () => void}) {
  const [items, setItems] = useState<MemoryEntry[]>([]), [target, setTarget] = useState<number>();
  const [reason, setReason] = useState(''), [error, setError] = useState(''), [conflict, setConflict] = useState(false);
  const [loading, setLoading] = useState(true), [saving, setSaving] = useState(false), [refresh, setRefresh] = useState(0);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    const controller = new AbortController();
    setLoading(true); setError(''); setConflict(false);
    /** 新读取需要重新选择结论，避免把已改变的正文当成用户原先确认的版本。 */
    async function load() {
      try {
        const value = await api.memoryConflicts(id, controller.signal);
        if (!controller.signal.aborted) { setItems(value.items); setTarget(undefined); }
      } catch (failure) { if (!controller.signal.aborted) setError(errorMessage(failure)); }
      finally { if (!controller.signal.aborted) setLoading(false); }
    }
    void load();
    return () => { mounted.current = false; controller.abort(); };
  }, [api, id, refresh]);

  /** 失败保持选择与原因，不偷偷替换期望版本；成功才由父页面重新读取当前事实。 */
  async function resolve() {
    if (target === undefined || !reason.trim() || saving || loading) return;
    setSaving(true); setError(''); setConflict(false);
    try {
      await api.resolveMemories({target_id: target, reason: reason.trim(),
        items: items.map(item => ({id: item.id, expected_revision: item.revision}))});
      if (mounted.current) onResolved();
    } catch (failure) {
      if (mounted.current) {
        setError(errorMessage(failure));
        setConflict(failure instanceof ApiFailure && failure.status === 409);
      }
    } finally { if (mounted.current) setSaving(false); }
  }

  return <section aria-label="冲突候选" className="catalog-form knowledge-form">
    <h3>核对冲突结论</h3><p>请选择采用的一项结论，其他候选将标为失效，原文保留在历史中。</p>
    {error && <Alert type="error" showIcon message={error} />}
    {conflict && <p>选择和原因已保留，请重新载入冲突后核对最新正文与版本，再明确选择。</p>}
    {loading ? <p role="status">正在读取整组冲突…</p> : items.map(item => <article key={item.id}>
      <label><input type="radio" name="memory-conflict-choice" aria-label={'采用' + item.key}
        checked={target === item.id} disabled={saving} onChange={() => setTarget(item.id)} />采用此结论</label>
      <MemoryFacts memory={item} />
    </article>)}
    {!loading && items.length < 2 && <p>当前没有可共同处理的冲突候选，请重新检查记忆状态。</p>}
    <label>解决原因<textarea required maxLength={300} disabled={saving} value={reason} onChange={event => setReason(event.target.value)} /></label>
    <div className="dialog-actions"><Button disabled={loading || saving} onClick={() => setRefresh(value => value + 1)}>重新载入冲突</Button>
      <Button type="primary" loading={saving} disabled={loading || target === undefined || !reason.trim() || items.length < 2}
        onClick={() => void resolve()}>确认采用结论</Button></div>
  </section>;
}
