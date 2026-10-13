import { Tabs } from 'antd';
import { Agents } from './Agents';
import { Skills } from './Skills';
import { McpServers } from './McpServers';

/** Agent、Skill 与 MCP 共用入口；切换后重新读取目录，及时更新关联选项。 */
export function AgentManagement({ token }: { token: string }) {
  return <Tabs className="agent-management" defaultActiveKey="agents" destroyOnHidden
    items={[
      { key: 'agents', label: 'Agent', children: <Agents token={token} /> },
      { key: 'skills', label: 'Skill', children: <Skills token={token} /> },
      { key: 'mcps', label: 'MCP', children: <McpServers token={token} /> },
    ]} />;
}
