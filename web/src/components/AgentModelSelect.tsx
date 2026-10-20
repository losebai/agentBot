import { Alert, Button } from 'antd';
import { ModelSelect } from './ModelSelect';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AiApi } from '../ai-api';
import { errorMessage } from '../api';
import type { AiModel, Page } from '../ai-types';

/** 三种角色仅展示启用模型并允许复用，停用的已有绑定保持原值供管理员明确替换。 */
export function AgentModelSelect({ token, value, onChange, visionValue, fallbackValue,
  onVisionChange, onFallbackChange }: {
  token: string; value: number; onChange: (value: number) => void;
  visionValue?: number | null; fallbackValue?: number | null;
  onVisionChange?: (value: number | null) => void;
  onFallbackChange?: (value: number | null) => void;
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

  const disabledIds = new Set(page.items.filter(model => !model.enabled).map(model => model.id));
  const selectedRoutes = [
    { label: onVisionChange ? '聊天模型' : '默认模型', id: value },
    ...(onVisionChange ? [{ label: '图片理解模型', id: visionValue }] : []),
    ...(onFallbackChange ? [{ label: '错误转发模型', id: fallbackValue }] : []),
  ];

  return <>
    {error && <Alert type="error" showIcon message={'模型列表读取失败：' + error} />}
    <fieldset><legend>模型路由</legend>
    {selectedRoutes.filter(route => route.id && disabledIds.has(route.id)).map(route =>
      <p key={route.label} className="field-help">{route.label}已停用，请重新选择已启用的模型。</p>)}
    {onVisionChange && onFallbackChange && <p className="field-help">聊天模型、图片理解模型和错误转发模型可以选择同一个模型，图片理解模型仍须启用图片理解能力。</p>}
    <label>{onVisionChange ? '聊天模型' : '默认模型'}<ModelSelect ariaLabel={onVisionChange ? '聊天模型' : '默认模型'} numeric required
      models={page.items} value={value} disabled={busy} loading={busy} onChange={next => onChange(Number(next))} /></label>
    {onVisionChange && <label>图片理解模型<ModelSelect ariaLabel="图片理解模型" numeric nullable vision
      models={page.items} value={visionValue ?? null} disabled={busy} loading={busy}
      placeholder="未配置：带图请求将报错" onChange={next => onVisionChange(next === null ? null : Number(next))} /></label>}
    {onVisionChange && <p className="field-help">带图请求必须配置图片理解模型，留空会报错。无法选择的模型尚未启用图片理解。请先在“供应商与模型”的模型列表中开启“图片理解”，开关会立即保存，再点击下方“刷新模型状态”。</p>}
    {onFallbackChange && <label>错误转发模型<ModelSelect ariaLabel="错误转发模型" numeric nullable
      models={page.items} value={fallbackValue ?? null} disabled={busy} loading={busy}
      placeholder="不启用错误转发" onChange={next => onFallbackChange(next === null ? null : Number(next))} /></label>}
    {onFallbackChange && <p className="field-help">模型失败时由备用模型接管一次，继续使用已有工具结果。与本次使用的模型相同时不会重复请求同一模型。带图请求的备用模型也需启用图片理解。</p>}
    </fieldset>
    <div className="heading-actions">
      <Button loading={busy} onClick={() => void load()}>刷新模型状态</Button>
      {page.next_cursor && <Button disabled={busy} onClick={() => void load(page.next_cursor!)}>加载更多模型</Button>}
    </div>
  </>;
}
