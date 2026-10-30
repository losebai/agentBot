import { DownOutlined, RightOutlined, RobotOutlined } from '@ant-design/icons';
import { useEffect, useState } from 'react';

/** 角色配置和自我演进共享一级分组，入口只负责展开，不触发页面数据读取。 */
export function AgentMenu({ page, onNavigate }: {
  page: string; onNavigate: (page: string) => void;
}) {
  const [expanded, setExpanded] = useState(true);
  useEffect(() => {
    if (page === 'agents' || page === 'agent-evolution') setExpanded(true);
  }, [page]);
  return <div className="plugin-navigation">
    <button type="button" aria-expanded={expanded} aria-controls="agent-submenu"
      onClick={() => setExpanded(value => !value)}>
      <RobotOutlined />角色和人格<span className="plugin-menu-arrow">{expanded ? <DownOutlined /> : <RightOutlined />}</span>
    </button>
    {expanded && <div id="agent-submenu" className="plugin-submenu" role="group" aria-label="角色和人格子菜单">
      {[{ key: 'agents', label: '角色配置' }, { key: 'agent-evolution', label: '自我演进' }].map(item => (
        <button type="button" key={item.key} className={page === item.key ? 'active' : ''}
          aria-current={page === item.key ? 'page' : undefined}
          onClick={() => onNavigate(item.key)}>{item.label}</button>
      ))}
    </div>}
  </div>;
}
