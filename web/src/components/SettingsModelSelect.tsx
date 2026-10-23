import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AiApi } from '../ai-api';
import { errorMessage } from '../api';
import type { AiModel, Page } from '../ai-types';
import { ambiguousModelReference, ModelSelect, selectedModel } from './ModelSelect';

export const modelSettingNames = new Set([
  'chat_model', 'chat_vision_model', 'chat_image_model', 'chat_tts_model',
  'chat_memory_embedding_model',
  'chat_auto_speak_decision_model',
]);

/** 模型配置字段共享目录，只在模型设置可见时读取；分类切换和凭据变化取消旧请求。 */
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
        items: [...(cursor ? previous.items : []), ...result.items],
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

/** 旧别名可读，新选择写稳定引用；加载及失败不改变父页面的配置草稿。 */
export function SettingsModelSelect({ id, value, readOnly, nullable, directory, onChange }: {
  id: string; value: string; readOnly: boolean; nullable: boolean;
  directory: ReturnType<typeof useSettingsModels>; onChange: (value: string) => void;
}) {
  const { page, busy, error } = directory;
  const current = selectedModel(page.items, value);
  const missing = !!value && (!current || !current.enabled);
  const ambiguous = !!value && ambiguousModelReference(page.items, value);
  return <>
    <ModelSelect id={id} models={page.items} value={value} disabled={readOnly || busy} loading={busy}
      nullable={nullable} placeholder={nullable ? '未配置' : '选择模型'}
      describedBy={missing ? `${id}-help` : undefined} onChange={next => onChange(String(next ?? ''))} />
    {missing && !busy && <small id={`${id}-help`}>{ambiguous ? '当前配置存在模型身份歧义，请先完成模型身份升级。' : error
      ? '当前配置尚未确认，请刷新模型列表。'
      : page.next_cursor ? '当前配置不在已加载的启用模型中，请加载更多或重新选择。'
        : '当前配置的模型未启用或不存在，请重新选择。'}</small>}
  </>;
}
