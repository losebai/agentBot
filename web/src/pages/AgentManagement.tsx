import { Agents } from './Agents';

/** 保留角色草稿，返回页面时同步运行角色排序；演进与远程服务由各自入口维护。 */
export function AgentManagement({ token, active = true }: { token: string; active?: boolean }) {
  return <Agents token={token} active={active} />;
}
