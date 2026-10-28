import { useEffect, useMemo, useState } from 'react';
import { ApiClient, errorMessage } from './api';
import type { PluginInfo } from './types';

const pendingState = { runningPlugins: new Set<string>() as ReadonlySet<string>,
  pluginsLoading: true, pluginsError: '' };

// 独立配置页由本地组件明确实现，服务端目录不能让页面加载任意模块。
export const pluginConfigurationLabels = {
  qqbot: 'QQ 配置', bilibili: 'B站配置', comfy: 'Comfy 配置',
} as const;
export type ConfigurablePlugin = keyof typeof pluginConfigurationLabels;

/** 只有已实现配置组件的插件才提供配置跳转。 */
export function hasPluginConfiguration(id: string): id is ConfigurablePlugin {
  return Object.hasOwn(pluginConfigurationLabels, id);
}

/** 保存的启用草稿不代表插件已完成启动，界面能力仅跟随当前进程。 */
export function isPluginRunning(plugin: PluginInfo): boolean {
  return plugin.enabled && plugin.status === 'running';
}

/** 各入口独立读取插件运行状态；卸载和刷新时取消旧请求，不保留过期能力。 */
export function useRunningPlugins(token: string, revision: number | string = 0) {
  const api = useMemo(() => new ApiClient(token), [token]);
  const [state, setState] = useState(pendingState);
  useEffect(() => {
    const controller = new AbortController();
    setState(pendingState);
    api.plugins(controller.signal).then(items => {
      if (!controller.signal.aborted) setState({
        runningPlugins: new Set(items.filter(isPluginRunning).map(item => item.id)),
        pluginsLoading: false, pluginsError: '',
      });
    }).catch(failure => {
      if (!controller.signal.aborted) setState({ runningPlugins: new Set(),
        pluginsLoading: false, pluginsError: errorMessage(failure) });
    });
    return () => controller.abort();
  }, [api, revision]);
  return state;
}
