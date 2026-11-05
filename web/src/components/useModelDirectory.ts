import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AiApi } from '../ai-api';
import { errorMessage } from '../api';
import type { AiModel, Page } from '../ai-types';

/** 模型选择共享目录；选择器后台读取完整目录，不在控件旁暴露列表分页。 */
export function useModelDirectory(token: string, enabled = true) {
  const api = useMemo(() => new AiApi(token), [token]);
  const [page, setPage] = useState<Page<AiModel>>({ items: [], next_cursor: null });
  const [busy, setBusy] = useState(true), [error, setError] = useState('');
  const pending = useRef<AbortController>();

  /** 每次刷新沿稳定游标读完目录；重复游标视为协议错误，避免异常上游形成死循环。 */
  const load = useCallback(async () => {
    pending.current?.abort();
    const current = new AbortController(); pending.current = current;
    setBusy(true); setError('');
    try {
      const items: AiModel[] = [];
      const ids = new Set<number>();
      const cursors = new Set<string>();
      let cursor: string | undefined;
      let result: Page<AiModel>;
      do {
        result = await api.models(cursor, undefined, current.signal);
        current.signal.throwIfAborted();
        for (const item of result.items) if (!ids.has(item.id)) { ids.add(item.id); items.push(item); }
        const next = result.next_cursor ?? undefined;
        if (next && cursors.has(next)) throw new Error('模型目录返回了重复分页游标');
        if (next) cursors.add(next);
        cursor = next;
      } while (cursor);
      if (!current.signal.aborted) {
        setPage({items, next_cursor: null});
      }
    } catch (failure) {
      if (!current.signal.aborted) setError(errorMessage(failure));
    } finally {
      if (!current.signal.aborted) setBusy(false);
    }
  }, [api]);

  useEffect(() => {
    setPage({ items: [], next_cursor: null }); setError('');
    if (enabled) void load();
    else setBusy(false);
    // 凭据变化、分类切换和卸载均取消目录请求，迟到响应不能覆盖新快照。
    return () => pending.current?.abort();
  }, [enabled, load]);
  return { page, busy, error, load };
}
