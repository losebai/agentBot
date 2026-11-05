import { Alert } from 'antd';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AgentApi } from '../agent-api';
import { errorMessage } from '../api';
import type { Agent } from '../agent-types';

import { ChoiceSelect } from './ChoiceSelect';

/** 运行配置只保存角色代码；读取完整目录，旧值失效时保留显示且不静默清空。 */
export function SettingsAgentSelect({ token, id, value, readOnly, onChange }: {
  token: string; id: string; value: string; readOnly: boolean; onChange: (value: string) => void;
}) {
  const api = useMemo(() => new AgentApi(token), [token]);
  const [items, setItems] = useState<Agent[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const pending = useRef<AbortController>();
  /** 打开下拉时重新读取启用状态；取消旧请求避免较晚的响应覆盖新目录。 */
  const load = useCallback(async () => {
    pending.current?.abort();
    const controller = new AbortController();
    pending.current = controller;
    setBusy(true); setError('');
    try {
      const agents: Agent[] = [];
      const cursors = new Set<string>();
      let cursor: string | undefined;
      do {
        const page = await api.listAgents(cursor, controller.signal);
        controller.signal.throwIfAborted();
        agents.push(...page.items);
        cursor = page.next_cursor ?? undefined;
        if (cursor && cursors.has(cursor)) throw new Error('角色目录返回了重复分页游标');
        if (cursor) cursors.add(cursor);
      } while (cursor);
      setItems(agents);
    } catch (failure) {
      if (!controller.signal.aborted) setError(errorMessage(failure));
    } finally { if (!controller.signal.aborted) setBusy(false); }
  }, [api]);
  useEffect(() => { void load(); return () => pending.current?.abort(); }, [load]);
  const selected = items.find(agent => agent.code === value);
  return <>
    <ChoiceSelect id={id} value={value} disabled={readOnly} loading={busy} onChange={onChange}
      onOpenChange={open => { if (open) void load(); }}>
      <option value="">默认：使用当前聊天模型与提示词</option>
      {value && !selected && <option value={value} disabled>保留当前配置（{value}）</option>}
      {items.map(agent => <option key={agent.id} value={agent.code} disabled={!agent.enabled}>
        {`${agent.name}（${agent.code}）${agent.enabled ? '' : ' · 已停用'}`}</option>)}
    </ChoiceSelect>
    {!busy && !error && value && !selected?.enabled && <small>当前角色已停用或不存在，请选择已启用的角色或默认配置。</small>}
    {error && <Alert type="error" showIcon message={error} />}
  </>;
}
