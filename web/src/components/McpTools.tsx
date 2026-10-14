import { Alert, Button, Switch } from 'antd';
import { useEffect, useRef, useState } from 'react';
import type { AgentApi } from '../agent-api';
import type { McpServer, McpTool } from '../agent-types';
import { errorMessage } from '../api';

/** 发现完整工具列表，启用状态只修改表单草稿，保存时统一进行版本校验。 */
export function McpTools({ api, server, connectionChanged, allowedTools, onChange }: {
  api: AgentApi; server: McpServer | null; connectionChanged: boolean;
  allowedTools: string[]; onChange: (tools: string[]) => void;
}) {
  const [tools, setTools] = useState<McpTool[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const controller = useRef<AbortController | null>(null);

  useEffect(() => {
    setTools(null); setError(''); setBusy(false);
    return () => controller.current?.abort();
  }, [api, server?.id, server?.revision, connectionChanged]);

  /** 只读取保存后的连接；弹窗关闭或连接字段改变时丢弃旧响应。 */
  async function load() {
    if (!server || connectionChanged) return;
    controller.current?.abort();
    const request = new AbortController();
    controller.current = request;
    setBusy(true); setError('');
    try {
      const discovered = await api.listMcpTools(server.id, request.signal);
      if (!request.signal.aborted) setTools(discovered);
    } catch (failure) {
      if (!request.signal.aborted) setError(errorMessage(failure));
    } finally {
      if (!request.signal.aborted) setBusy(false);
    }
  }

  /** 关闭通配符中的单项时展开当前全部名称，保证关闭的工具不会继续被星号授权。 */
  function toggle(name: string, enabled: boolean) {
    const current = allowedTools.includes('*') ? (tools ?? []).map(tool => tool.name) : allowedTools;
    onChange(enabled ? Array.from(new Set([...current, name])) : current.filter(tool => tool !== name));
  }

  return <section aria-label="MCP 工具管理">
    <div className="heading-actions">
      <Button loading={busy} disabled={!server || connectionChanged} onClick={() => void load()}>获取全部工具</Button>
      <Button disabled={busy || !tools?.length} onClick={() => onChange(['*'])}>启用全部工具</Button>
      <Button disabled={busy || !tools?.length} onClick={() => onChange([])}>关闭全部工具</Button>
    </div>
    {(!server || connectionChanged) && <p>请先保存连接配置，再获取全部工具。</p>}
    {error && <Alert type="error" showIcon message={'工具列表获取失败：' + error} />}
    {tools && <>
      <p>已获取 {tools.length} 个工具，启用 {tools.filter(tool => allowedTools.includes('*') || allowedTools.includes(tool.name)).length} 个。开关修改在保存 MCP 后生效。</p>
      {tools.length > 32 && <p>单次执行最多使用 32 个 MCP 工具，请按需选择要启用的工具。</p>}
      <div className="table-wrap" style={{ maxHeight: 360, overflow: 'auto' }}>
        <table className="data-table" aria-label="MCP 工具列表">
          <thead><tr><th>工具名称</th><th>说明</th><th>启用</th></tr></thead>
          <tbody>{tools.map(tool => <tr key={tool.name}>
            <td><strong>{tool.name}</strong></td><td>{tool.description || '—'}</td>
            <td><Switch aria-label={`启用工具 ${tool.name}`} disabled={busy}
              checked={allowedTools.includes('*') || allowedTools.includes(tool.name)}
              onChange={enabled => toggle(tool.name, enabled)} /></td>
          </tr>)}</tbody>
        </table>
      </div>
    </>}
  </section>;
}
