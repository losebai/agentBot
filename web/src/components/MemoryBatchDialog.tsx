import { Button } from 'antd';
import { useEffect, useRef, useState } from 'react';
import { ApiFailure, errorMessage } from '../api';
import type { KnowledgeApi } from '../knowledge-api';
import type { MemoryEntry, MemoryMerge, MemoryPreview } from '../knowledge-types';
import { CatalogDialog } from './CatalogDialog';
import { memoryScopeLabel, memoryStatusNames } from './MemoryFacts';

export type MemoryBatch = {action: 'merge'; items: MemoryEntry[]}
  | {action: 'expire' | 'forget'; scope_id: string; topic: string};

/** 批量写入固定在服务端预览的 ID 和版本，不能把随后新增的同主题事实一起处理。 */
export function MemoryBatchDialog({api, batch, onClose, onSaved}: {
  api: KnowledgeApi; batch: MemoryBatch; onClose: () => void; onSaved: () => void;
}) {
  const merging = batch.action === 'merge';
  const [target, setTarget] = useState(merging ? batch.items[0].id : 0);
  const [content, setContent] = useState(''), [reason, setReason] = useState('');
  const [preview, setPreview] = useState<MemoryPreview>();
  const [busy, setBusy] = useState(false), [writing, setWriting] = useState(false), [error, setError] = useState('');
  const mounted = useRef(true), request = useRef<AbortController>();
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; request.current?.abort(); };
  }, []);

  /** 合并请求在预览前后保持同一正文和理由；编辑这些字段会使预览失效。 */
  function mergeBody(): MemoryMerge {
    return {target_id: target, items: (preview?.items || (merging ? batch.items : [])).map(item => ({id: item.id, expected_revision: item.revision})),
      reason: reason.trim(), ...(content.trim() ? {content} : {})};
  }

  /** 预览只有读取语义，可关闭取消；正式提交开始后保持弹窗等待明确结果。 */
  async function loadPreview() {
    if (busy) return;
    const controller = new AbortController(); request.current?.abort(); request.current = controller;
    setBusy(true); setError('');
    try {
      const value = merging ? await api.previewMemoryMerge(mergeBody(), controller.signal)
        : await api.previewMemoryCleanup({scope_id: batch.scope_id, topic: batch.topic, action: batch.action}, controller.signal);
      if (!controller.signal.aborted) setPreview(value);
    } catch (failure) { if (!controller.signal.aborted) setError(errorMessage(failure)); }
    finally { if (!controller.signal.aborted) setBusy(false); }
  }

  useEffect(() => {
    if (!merging) void loadPreview();
    // 清理条件由父组件固定，预览只在本弹窗首次打开时读取。
  }, []);

  /** 冲突后丢弃预览而保留操作说明，必须重新预览后才允许再次确认。 */
  async function apply() {
    if (!preview || busy || !reason.trim()) return;
    setBusy(true); setWriting(true); setError('');
    try {
      if (merging) await api.mergeMemories({...mergeBody(), digest: preview.digest});
      else await api.cleanupMemories({items: preview.items.map(item => ({id: item.id, expected_revision: item.revision})),
        action: batch.action, digest: preview.digest, reason: reason.trim()});
      if (mounted.current) onSaved();
    } catch (failure) {
      if (mounted.current) {
        setError(errorMessage(failure));
        if (failure instanceof ApiFailure && failure.status === 409) setPreview(undefined);
      }
    } finally { if (mounted.current) { setBusy(false); setWriting(false); } }
  }

  return <CatalogDialog title={merging ? '合并记忆' : '主题清理预览'} busy={writing} error={error} onCancel={onClose}>
    <div className="catalog-form knowledge-form">
      {merging ? <>
        <label>保留为目标记忆<select disabled={busy || !!preview} value={target} onChange={event => { setTarget(Number(event.target.value)); setPreview(undefined); }}>
          {batch.items.map(item => <option key={item.id} value={item.id}>{item.key} · {memoryScopeLabel(item)}</option>)}</select></label>
        <label>合并后的正文（留空保留目标正文）<textarea disabled={busy || !!preview} rows={5} maxLength={1000} value={content} onChange={event => { setContent(event.target.value); setPreview(undefined); }} /></label>
        <p>仅合并同一作用域、同一主体的重复事实。来源与历史由服务端保留，其他事实不会因为主题相同就自动合并。</p>
      </> : <p>主题：{batch.topic}。操作：{batch.action === 'expire' ? '标为失效' : '遗忘'}。只处理以下预览版本。</p>}
      {preview && <>
        <p>本次影响 {preview.items.length} 条记忆。</p>
        <ul>{preview.items.map(item => <li key={item.id}><strong>{item.key} · 第 {item.revision} 版</strong>
          <p>{memoryScopeLabel(item)} · {item.topic} · {memoryStatusNames[item.effective_status || item.status]}</p>
          <pre className="knowledge-content">{item.content}</pre>
        </li>)}</ul>
      </>}
      {batch.action === 'forget' && <p>遗忘后停止长期记忆召回，仍保留管理员审计历史。本轮不提供恢复功能。</p>}
      <label>操作原因<textarea required maxLength={300} disabled={busy || (merging && !!preview)} value={reason}
        onChange={event => { setReason(event.target.value); if (merging) setPreview(undefined); }} /></label>
      <div className="dialog-actions"><Button disabled={writing} onClick={onClose}>取消</Button>
        {preview ? <>
          <Button disabled={busy} onClick={() => setPreview(undefined)}>重新预览</Button>
          <Button type="primary" danger={batch.action === 'forget'} loading={busy} disabled={!reason.trim() || !preview.items.length}
            onClick={() => void apply()}>{merging ? '确认合并' : '确认清理'}</Button>
        </> : <Button type="primary" loading={busy} disabled={merging && !reason.trim()} onClick={() => void loadPreview()}>{merging ? '预览合并' : '重新获取清理预览'}</Button>}
      </div>
    </div>
  </CatalogDialog>;
}
