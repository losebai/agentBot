import { Agents } from './Agents';

/** 角色和人格直接展示 Agent 配置，远程服务通过独立一级导航维护。 */
export function AgentManagement({ token }: { token: string }) {
  return <Agents token={token} />;
}
