import { Alert, Button, Select } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import { AgentApi } from '../agent-api';
import { errorMessage } from '../api';
import type { McpServer } from '../agent-types';
import { useRunningPlugins } from '../plugin-runtime';

// 内置名称对应服务端工具策略；选择只保存配置，不授予当前 QQ 会话执行权限。
const builtinTools = [
  ['bilibili_login_status', 'B站登录检测'], ['bilibili_get_my_videos', 'B站我的视频'],
  ['bilibili_get_comments', 'B站评论读取'],
  ['bilibili_reply_comment', 'B站回复评论'], ['bilibili_publish_video', 'B站视频投稿'],
  ['bilibili_publish_dynamic', 'B站发布图文动态'],
  ['read_url', '网页读取'],
  ['knowledge_search', '知识库检索'],
  ['material_search', '素材查询'], ['material_get', '素材读取'],
  ['memory', '标签记忆（QQ）'], ['naraka_query', '永劫无间查询（QQ）'],
  ['bot_config_get', '查询机器人配置（仅主人）'],
  ['bot_config_update', '修改机器人配置（仅主人）'],
  ['naraka_search', '永劫无间搜索（QQ）'], ['mention_member', '群成员 @（QQ）'],
  ['image_generate', '模型直接生图'],
  ['local_workflow_list', '工作流列表'], ['local_workflow_generate_image', '工作流生图'], ['local_workflow_generate_video', '工作流生视频'],
  ['local_workflow_tasks', '绘图任务列表（QQ）'], ['local_workflow_cancel', '取消绘图任务（QQ）'],
  ['tts_generate', '语音生成'], ['job', '后台任务（QQ）'],
];

// 共享媒体与知识工具由公共核心提供；平台专属工具须等所属插件启动。
const pluginTools: Record<string, string> = {
  bilibili_login_status: 'bilibili', bilibili_get_my_videos: 'bilibili', bilibili_get_comments: 'bilibili',
  bilibili_reply_comment: 'bilibili', bilibili_publish_video: 'bilibili',
  bilibili_publish_dynamic: 'bilibili',
  memory: 'qqbot', naraka_query: 'qqbot', naraka_search: 'qqbot', mention_member: 'qqbot',
  // 配置工具的可见性随 QQ 插件运行，实际执行还须服务端校验主人身份。
  bot_config_get: 'qqbot', bot_config_update: 'qqbot',
  local_workflow_tasks: 'qqbot', local_workflow_cancel: 'qqbot', job: 'qqbot',
};

/** 平台工具随插件实际运行状态展示，已有配置在停用期间保留。 */
export function AgentToolSelects({ token, tools, mcpIds, onToolsChange, onMcpsChange }: {
  token: string; tools: string[]; mcpIds: number[];
  onToolsChange: (tools: string[]) => void; onMcpsChange: (ids: number[]) => void;
}) {
  const api = useMemo(() => new AgentApi(token), [token]);
  const [servers, setServers] = useState<McpServer[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [refresh, setRefresh] = useState(0);
  const { runningPlugins, pluginsLoading, pluginsError } = useRunningPlugins(token, refresh);

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

  // 分类仅用于展示，搜索类 MCP 与其他服务使用同一关联和白名单规则。
  const selected = servers.filter(server => mcpIds.includes(server.id));
  const toolVisible = (name: string) => !Object.hasOwn(pluginTools, name) || runningPlugins.has(pluginTools[name]);
  const toolOptions = new Map(builtinTools.filter(([value]) => toolVisible(value))
    .map(([value, name]) => [value, { value, label: name + ' · ' + value }]));
  // 保留历史手动配置，关联 MCP 的新工具由服务端发现，不要求重复勾选。
  for (const name of tools) if (toolVisible(name) && !toolOptions.has(name)) toolOptions.set(name, { value: name, label: name + '（原有配置）' });
  const mcpOptions = servers.map(server => ({
    value: server.id, label: server.name + ' · ' + server.code + (server.enabled ? '' : '（停用）'),
    disabled: !mcpIds.includes(server.id) && mcpIds.length >= 3,
  }));
  for (const id of mcpIds) if (!servers.some(server => server.id === id)) {
    mcpOptions.push({ value: id, label: 'MCP #' + id + '（未在当前目录）', disabled: false });
  }

  return <>
    {error && <Alert type="error" showIcon message={'MCP 列表读取失败：' + error} />}
    {pluginsError && <Alert type="error" showIcon message={'插件状态读取失败：' + pluginsError} />}
    <label htmlFor="agent-mcp-select">关联 MCP（最多 3 个）</label>
    <Select id="agent-mcp-select" aria-label="关联 MCP（最多 3 个）" mode="multiple" showSearch allowClear
      loading={loading} disabled={loading} value={mcpIds} options={mcpOptions} optionFilterProp="label"
      placeholder="搜索并选择 MCP 服务" style={{ width: '100%' }}
      onChange={ids => { if (ids.length <= 3) onMcpsChange(ids); }} />
    <div className="heading-actions"><Button loading={loading} onClick={() => setRefresh(value => value + 1)}>刷新 MCP 列表</Button></div>
    <label htmlFor="agent-tools-select">允许工具</label>
    <Select id="agent-tools-select" aria-label="允许工具" mode="multiple" showSearch allowClear
      loading={pluginsLoading} value={tools.filter(toolVisible)} options={Array.from(toolOptions.values())} optionFilterProp="label"
      placeholder="搜索并选择工具" style={{ width: '100%' }} onChange={next => {
        // 修改当前可见工具时不覆盖未运行插件的历史配置，重新启动后仍可使用。
        onToolsChange([...tools.filter(name => !toolVisible(name)), ...next]);
      }} />
    {tools.some(name => !toolVisible(name)) && <p>未运行插件的工具配置已保留，插件启动后显示。</p>}
    <p>此处选择内置工具，QQ 工具仍需当前会话授权。已关联 MCP 的工具自动可用，无需在此逐个选择；具体范围由 MCP 服务白名单决定。</p>
    {selected.some(server => server.allowed_tools.includes('*')) && <p>白名单为 * 时自动使用服务返回的全部工具。</p>}
  </>;
}
