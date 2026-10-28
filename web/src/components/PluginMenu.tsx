import { ApiOutlined, DownOutlined, RightOutlined } from '@ant-design/icons';
import { useEffect, useState } from 'react';
import { pluginConfigurationLabels, useRunningPlugins } from '../plugin-runtime';
import type { ConfigurablePlugin } from '../plugin-runtime';

/** 二级配置入口只展示运行中的本地插件，每次导航重新核对进程状态。 */
export function PluginMenu({ token, page, onNavigate }: {
  token: string; page: string; onNavigate: (page: string) => void;
}) {
  const [expanded, setExpanded] = useState(true);
  const { runningPlugins, pluginsError } = useRunningPlugins(token, page);
  useEffect(() => {
    if (page.startsWith('plugin')) setExpanded(true);
  }, [page]);
  const items = [
    { key: 'plugin-center', label: '插件中心' },
    { key: 'plugins', label: '已安装插件' },
    ...(Object.keys(pluginConfigurationLabels) as ConfigurablePlugin[])
      .filter(id => runningPlugins.has(id))
      .map(id => ({ key: `plugin-${id}`, label: pluginConfigurationLabels[id] })),
  ];
  return <div className="plugin-navigation">
    <button type="button" aria-expanded={expanded} aria-controls="plugin-submenu"
      onClick={() => setExpanded(value => !value)}>
      <ApiOutlined />插件管理<span className="plugin-menu-arrow">{expanded ? <DownOutlined /> : <RightOutlined />}</span>
    </button>
    {expanded && <div id="plugin-submenu" className="plugin-submenu" role="group" aria-label="插件管理子菜单">
      {items.map(item => <button type="button" key={item.key} className={page === item.key ? 'active' : ''}
        aria-current={page === item.key ? 'page' : undefined} onClick={() => onNavigate(item.key)}>{item.label}</button>)}
      {pluginsError && <span role="alert" className="plugin-menu-error">插件状态读取失败，请刷新页面</span>}
    </div>}
  </div>;
}
