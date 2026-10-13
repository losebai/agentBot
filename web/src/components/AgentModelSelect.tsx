import { Alert, Button } from 'antd';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AiApi } from '../ai-api';
import { errorMessage } from '../api';
import type { AiModel, Page } from '../ai-types';

/** 编辑框每次打开均读取模型状态，避免跨页面启用模型后仍显示旧快照。 */
export function AgentModelSelect({ token, value, onChange }: {
  token: string; value: number; onChange: (value: number) => void;
}) {
  const api = useMemo(() => new AiApi(token), [token]);
  const [page, setPage] = useState<Page<AiModel>>({ items: [], next_cursor: null });
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState('');
  const pending = useRef<AbortController | null>(null);

  /** 刷新替换快照，翻页追加；取消旧查询，防止较晚返回的响应覆盖新状态。 */
  const load = useCallback(async (cursor?: string) => {
    pending.current?.abort();
    const current = new AbortController();
    pending.current = current;
    setBusy(true); setError('');
    if (!cursor) setPage({ items: [], next_cursor: null });
    try {
      const result = await api.models(cursor, undefined, current.signal);
      if (!current.signal.aborted) setPage(previous => ({
        items: cursor ? [...previous.items, ...result.items] : result.items,
        next_cursor: result.next_cursor,
      }));
    } catch (failure) {
      if (!current.signal.aborted) setError(errorMessage(failure));
    } finally {
      if (!current.signal.aborted) setBusy(false);
    }
  }, [api]);

  useEffect(() => {
    void load();
    // 弹窗关闭即释放读取；草稿所选 ID 由父表单保存，不以加载失败清空它。
    return () => pending.current?.abort();
  }, [load]);

  return <>
    {error && <Alert type="error" showIcon message={'模型列表读取失败：' + error} />}
    <label>默认模型<select required value={value || ''} disabled={busy}
      onChange={event => onChange(Number(event.target.value))}>
      <option value="">{busy ? '正在加载模型…' : '选择模型'}</option>
      {!!value && !page.items.some(model => model.id === value) &&
        <option value={value}>模型 #{value}（尚未加载）</option>}
      {page.items.map(model => <option key={model.id} value={model.id}>
        {model.alias}{model.enabled ? '' : '（停用）'}
      </option>)}
    </select></label>
    <div className="heading-actions">
      <Button loading={busy} onClick={() => void load()}>刷新模型状态</Button>
      {page.next_cursor && <Button disabled={busy} onClick={() => void load(page.next_cursor!)}>加载更多模型</Button>}
    </div>
  </>;
}
