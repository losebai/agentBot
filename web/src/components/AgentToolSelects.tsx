import { Alert, Button, Select } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import { AgentApi } from '../agent-api';
import { errorMessage } from '../api';
import type { McpServer } from '../agent-types';

// 内置名称对应服务端工具策略；选择只保存配置，不授予当前 QQ 会话执行权限。
const builtinTools = [
  ['web_search', '网页搜索'], ['read_url', '网页读取'],
  ['memory', '标签记忆（QQ）'], ['naraka_query', '永劫无间查询（QQ）'],
  ['naraka_search', '永劫无间搜索（QQ）'], ['mention_member', '群成员 @（QQ）'],
  ['local_workflow_list', '工作流列表'], ['local_workflow_generate_image', '工作流生图'],
  ['local_workflow_tasks', '绘图任务列表（QQ）'], ['local_workflow_cancel', '取消绘图任务（QQ）'],
  ['tts_generate', '语音生成'], ['job', '后台任务（QQ）'],
];

/** 每次打开编辑框读取完整 MCP 目录，工具选项来自内置目录及已选 MCP 白名单。 */
export function AgentToolSelects({ token, tools, mcpIds, onToolsChange, onMcpsChange }: {
  token: string; tools: string[]; mcpIds: number[];
  onToolsChange: (tools: string[]) => void; onMcpsChange: (ids: number[]) => void;
}) {
  const api = useMemo(() => new AgentApi(token), [token]);
  const [servers, setServers] = useState<McpServer[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError(''); setServers([]);
    /** 自动读取全部分页，搜索能覆盖目录末尾；关闭弹窗停止后续查询。 */
    async function load() {
      try {
        const all: McpServer[] = [];
        let cursor: string | undefined;
        do {
          const page = await api.listMcps(cursor, controller.signal);
          if (controller.signal.aborted) return;
          all.push(...page.items); cursor = page.next_cursor || undefined;
        } while (cursor);
        setServers(all);
      } catch (failure) { if (!controller.signal.aborted) setError(errorMessage(failure)); }
      finally { if (!controller.signal.aborted) setLoading(false); }
    }
    void load();
    return () => controller.abort();
  }, [api, refresh]);

  const general = servers.filter(server => server.category === 'general');
  const selected = general.filter(server => mcpIds.includes(server.id));
  const toolOptions = new Map(builtinTools.map(([value, name]) => [value, { value, label: name + ' · ' + value }]));
  for (const server of selected) {
    for (const name of server.allowed_tools) {
      // 通配符是 MCP 白名单规则，不是可以传给 Agent 的工具名称。
      if (name !== '*' && !toolOptions.has(name)) toolOptions.set(name, { value: name, label: name + ' · ' + server.name });
    }
  }
  // 不因目录缺项或取消 MCP 关联静默修改已有工具授权，由用户明确移除。
  for (const name of tools) if (!toolOptions.has(name)) toolOptions.set(name, { value: name, label: name + '（原有配置）' });
  const mcpOptions = general.map(server => ({
    value: server.id, label: server.name + ' · ' + server.code + (server.enabled ? '' : '（停用）'),
    disabled: !mcpIds.includes(server.id) && mcpIds.length >= 3,
  }));
  for (const id of mcpIds) if (!general.some(server => server.id === id)) {
    mcpOptions.push({ value: id, label: 'MCP #' + id + '（未在当前目录）', disabled: false });
  }

  return <>
    {error && <Alert type="error" showIcon message={'MCP 列表读取失败：' + error} />}
    <label htmlFor="agent-mcp-select">关联 MCP（最多 3 个）</label>
    <Select id="agent-mcp-select" aria-label="关联 MCP（最多 3 个）" mode="multiple" showSearch allowClear
      loading={loading} disabled={loading} value={mcpIds} options={mcpOptions} optionFilterProp="label"
      placeholder="搜索并选择 MCP 服务" style={{ width: '100%' }}
      onChange={ids => { if (ids.length <= 3) onMcpsChange(ids); }} />
    <div className="heading-actions"><Button loading={loading} onClick={() => setRefresh(value => value + 1)}>刷新 MCP 列表</Button></div>
    <label htmlFor="agent-tools-select">允许工具</label>
    <Select id="agent-tools-select" aria-label="允许工具" mode="multiple" showSearch allowClear
      value={tools} options={Array.from(toolOptions.values())} optionFilterProp="label"
      placeholder="搜索并选择工具" style={{ width: '100%' }} onChange={onToolsChange} />
    <p>工具来自内置目录和所选 MCP 的白名单。QQ 工具仍需当前会话授权。</p>
    {selected.some(server => server.allowed_tools.includes('*')) && <p>所选 MCP 使用通配符白名单；如需选择其中的工具，请先在 MCP 管理中填写具体工具名称。</p>}
  </>;
}
