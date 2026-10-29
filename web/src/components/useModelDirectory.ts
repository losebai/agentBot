import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AiApi } from '../ai-api';
import { errorMessage } from '../api';
import type { AiModel, Page } from '../ai-types';

/** 模型选择共享目录；重新打开时更新已浏览的分页，不改动调用方的模型绑定或草稿。 */
export function useModelDirectory(token: string, enabled = true) {
  const api = useMemo(() => new AiApi(token), [token]);
  const [page, setPage] = useState<Page<AiModel>>({ items: [], next_cursor: null });
  const [busy, setBusy] = useState(true), [error, setError] = useState('');
  const pending = useRef<AbortController>();
  const loadedPages = useRef(1);

  /** 刷新按最新游标重读已浏览页，原子替换快照；失败保留旧目录，下次打开可重新读取。 */
  const load = useCallback(async (cursor?: string) => {
    pending.current?.abort();
    const current = new AbortController(); pending.current = current;
    setBusy(true); setError('');
    try {
      let result = await api.models(cursor, undefined, current.signal);
      let count = 1;
      if (!cursor) {
        const items = [...result.items];
        while (count < loadedPages.current && result.next_cursor) {
          current.signal.throwIfAborted();
          result = await api.models(result.next_cursor, undefined, current.signal);
          items.push(...result.items); count += 1;
        }
        result = { ...result, items };
      }
      if (!current.signal.aborted) {
        const next = result;
        setPage(previous => ({ ...next, items: cursor ? [...previous.items, ...next.items] : next.items }));
        loadedPages.current = cursor ? loadedPages.current + 1 : count;
      }
    } catch (failure) {
      if (!current.signal.aborted) setError(errorMessage(failure));
    } finally {
      if (!current.signal.aborted) setBusy(false);
    }
  }, [api]);

  useEffect(() => {
    loadedPages.current = 1;
    setPage({ items: [], next_cursor: null }); setError('');
    if (enabled) void load();
    else setBusy(false);
    // 凭据变化、分类切换和卸载均取消目录请求，迟到响应不能覆盖新快照。
    return () => pending.current?.abort();
  }, [enabled, load]);
  return { page, busy, error, load };
}
