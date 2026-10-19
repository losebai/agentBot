import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AiApi } from '../ai-api';
import { errorMessage } from '../api';
import type { AiModel, Page } from '../ai-types';

export const modelSettingNames = new Set([
  'chat_model', 'chat_vision_model', 'chat_image_model', 'chat_tts_model',
]);

/** 四个配置字段共享目录，只在模型设置可见时读取；分类切换和凭据变化取消旧请求。 */
export function useSettingsModels(token: string, enabled: boolean) {
  const api = useMemo(() => new AiApi(token), [token]);
  const [page, setPage] = useState<Page<AiModel>>({ items: [], next_cursor: null });
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState('');
  const pending = useRef<AbortController>();

  /** 手动刷新替换目录，加载更多追加；失败保留已有目录与父页面的所有配置草稿。 */
  const load = useCallback(async (cursor?: string) => {
    pending.current?.abort();
    const current = new AbortController();
    pending.current = current;
    setBusy(true); setError('');
    try {
      const result = await api.models(cursor, undefined, current.signal);
      if (!current.signal.aborted) setPage(previous => ({
        items: [...(cursor ? previous.items : []), ...result.items.filter(model => model.enabled === true)],
        next_cursor: result.next_cursor,
      }));
    } catch (failure) {
      if (!current.signal.aborted) setError(errorMessage(failure));
    } finally {
      if (!current.signal.aborted) setBusy(false);
    }
  }, [api]);

  useEffect(() => {
    setPage({ items: [], next_cursor: null });
    setError('');
    if (enabled) void load();
    // 取消后检查本次信号，避免慢响应覆盖新分类或新凭据的目录。
    return () => pending.current?.abort();
  }, [enabled, load]);

  return { page, busy, error, load };
}

/** 保存模型别名；未知或停用值用隐藏占位保留，不展示停用别名、不自动更换模型。 */
export function SettingsModelSelect({ id, value, readOnly, nullable, directory, onChange }: {
  id: string; value: string; readOnly: boolean; nullable: boolean;
  directory: ReturnType<typeof useSettingsModels>; onChange: (value: string) => void;
}) {
  const { page, busy, error } = directory;
  const missing = !!value && !page.items.some(model => model.alias === value);
  return <>
    <select id={id} value={value} disabled={readOnly || busy}
      aria-describedby={missing ? `${id}-help` : undefined}
      onChange={event => onChange(event.target.value)}>
      <option value="" disabled={!nullable}>{nullable ? '未配置' : '选择模型'}</option>
      {missing && <option value={value} hidden disabled>保留当前配置，请选择启用的模型</option>}
      {page.items.map(model => <option key={model.id} value={model.alias}>{model.alias}</option>)}
    </select>
    {missing && !busy && <small id={`${id}-help`}>{error
      ? '当前配置尚未确认，请刷新模型列表。'
      : page.next_cursor ? '当前配置不在已加载的启用模型中，请加载更多或重新选择。'
        : '当前配置的模型未启用或不存在，请重新选择。'}</small>}
  </>;
}
