import { Alert, Button } from 'antd';
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

  const enabledModels = page.items.filter(model => model.enabled);
  const disabledIds = new Set(page.items.filter(model => !model.enabled).map(model => model.id));
  const selectedRoutes = [
    { label: onVisionChange ? '聊天模型' : '默认模型', id: value },
    ...(onVisionChange ? [{ label: '图片理解模型', id: visionValue }] : []),
    ...(onFallbackChange ? [{ label: '错误转发模型', id: fallbackValue }] : []),
  ];

  /** 未加载引用保留 ID；已停用引用只用隐藏占位承载原值，不能作为模型选项再次选择。 */
  function selectedPlaceholder(id?: number | null) {
    if (!id || enabledModels.some(model => model.id === id)) return null;
    const disabled = disabledIds.has(id);
    return <option value={id} disabled={disabled} hidden={disabled}>
      {disabled ? '当前模型已停用，请重新选择' : `模型 #${id}（尚未加载）`}
    </option>;
  }

  return <>
    {error && <Alert type="error" showIcon message={'模型列表读取失败：' + error} />}
    <fieldset><legend>模型路由</legend>
    {selectedRoutes.filter(route => route.id && disabledIds.has(route.id)).map(route =>
      <p key={route.label} className="field-help">{route.label}已停用，请重新选择已启用的模型。</p>)}
    {onVisionChange && onFallbackChange && <p className="field-help">聊天模型、图片理解模型和错误转发模型可以选择同一个模型，图片理解模型仍须启用图片理解能力。</p>}
    <label>{onVisionChange ? '聊天模型' : '默认模型'}<select required value={value || ''} disabled={busy}
      onChange={event => onChange(Number(event.target.value))}>
      <option value="">{busy ? '正在加载模型…' : '选择模型'}</option>
      {selectedPlaceholder(value)}
      {enabledModels.map(model => <option key={model.id} value={model.id}>
        {model.alias}
      </option>)}
    </select></label>
    {onVisionChange && <label>图片理解模型<select value={visionValue ?? ''} disabled={busy}
      onChange={event => onVisionChange(event.target.value ? Number(event.target.value) : null)}>
      <option value="">未配置：带图请求将报错</option>
      {selectedPlaceholder(visionValue)}
      {enabledModels.map(model => <option key={model.id} value={model.id}
        disabled={!model.media?.vision_enabled}>
        {model.alias}
        {model.media?.vision_enabled ? '（支持图片理解）' : '（未启用图片理解）'}
      </option>)}
    </select></label>}
    {onVisionChange && <p className="field-help">带图请求必须配置图片理解模型，留空会报错。无法选择的模型尚未启用图片理解。请先在“供应商与模型”的模型列表中开启“图片理解”，开关会立即保存，再点击下方“刷新模型状态”。</p>}
    {onFallbackChange && <label>错误转发模型<select value={fallbackValue ?? ''} disabled={busy}
      onChange={event => onFallbackChange(event.target.value ? Number(event.target.value) : null)}>
      <option value="">不启用错误转发</option>
      {selectedPlaceholder(fallbackValue)}
      {enabledModels.map(model => <option key={model.id} value={model.id}>
        {model.alias}
        {model.media?.vision_enabled ? '（支持图片理解）' : ''}
      </option>)}
    </select></label>}
    {onFallbackChange && <p className="field-help">模型失败时由备用模型接管一次，继续使用已有工具结果。与本次使用的模型相同时不会重复请求同一模型。带图请求的备用模型也需启用图片理解。</p>}
    </fieldset>
    <div className="heading-actions">
      <Button loading={busy} onClick={() => void load()}>刷新模型状态</Button>
      {page.next_cursor && <Button disabled={busy} onClick={() => void load(page.next_cursor!)}>加载更多模型</Button>}
    </div>
  </>;
}
