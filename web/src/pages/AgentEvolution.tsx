import { Alert, Button } from 'antd';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AgentApi } from '../agent-api';
import type { Agent } from '../agent-types';
import { errorMessage } from '../api';
import { AgentEvolution as EvolutionPanel } from '../components/AgentEvolution';

/** 默认管理通用演化；可选角色目录独立读取，失败不阻挡通用状态与记录。 */
export function AgentEvolutionPage({ token }: { token: string }) {
  const api = useMemo(() => new AgentApi(token), [token]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [selected, setSelected] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const pending = useRef<AbortController>();

  /** 首屏刷新只清除已删除的角色选择，绝不自动把通用范围切到首个角色。 */
  const load = useCallback(async () => {
    pending.current?.abort();
    const controller = new AbortController(); pending.current = controller;
    setLoading(true); setError('');
    try {
      const items:Agent[]=[];const ids=new Set<number>(),cursors=new Set<string>();let cursor:string|undefined;
      do{const page=await api.listAgents(cursor,controller.signal);controller.signal.throwIfAborted();for(const item of page.items)if(!ids.has(item.id)){ids.add(item.id);items.push(item);}const next=page.next_cursor??undefined;if(next&&cursors.has(next))throw new Error('Agent 目录返回了重复分页游标');if(next)cursors.add(next);cursor=next;}while(cursor);
      if (controller.signal.aborted) return;
      setAgents(items);
      setSelected(old => old === null || items.some(agent => agent.id === old) ? old : null);
    } catch (failure) {
      if (!controller.signal.aborted) setError(errorMessage(failure));
    } finally { if (!controller.signal.aborted) setLoading(false); }
  }, [api]);

  useEffect(() => {
    void load();
    return () => pending.current?.abort();
  }, [load]);

  return <div className="page-content agent-page">
    <div className="page-heading"><div><p className="eyebrow">AGENT WORKSPACE</p>
      <h1>自我演进</h1><p>查看通用表达习惯，也可选择关联角色管理专属习惯与变化依据。</p></div>
      <Button loading={loading} onClick={() => void load()}>刷新 Agent</Button>
    </div>
    {error && <Alert type="error" showIcon message={error} />}
    <div className="agent-switcher" role="group" aria-label="选择演进 Agent">
      <Button type={selected === null ? 'primary' : 'default'} aria-pressed={selected === null}
        onClick={() => setSelected(null)}>未关联角色（通用）</Button>
      {agents.map(agent => <Button key={agent.id} disabled={loading}
        type={agent.id === selected ? 'primary' : 'default'} aria-pressed={agent.id === selected}
        onClick={() => setSelected(agent.id)}>{agent.name}</Button>)}
    </div>
    {!loading && !error && !agents.length && <p>暂无关联角色，通用演化仍可正常使用。</p>}
    <EvolutionPanel key={`${token}-${selected}`} token={token} agentId={selected} />
  </div>;
}
