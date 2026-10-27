import { useEffect, useMemo, useState } from 'react';
import { ApiClient, errorMessage } from './api';
import type { PluginInfo } from './types';

const pendingState = { runningPlugins: new Set<string>() as ReadonlySet<string>,
  pluginsLoading: true, pluginsError: '' };

/** 保存的启用草稿不代表插件已完成启动，界面能力仅跟随当前进程。 */
export function isPluginRunning(plugin: PluginInfo): boolean {
  return plugin.enabled && plugin.status === 'running';
}

/** 各入口独立读取插件运行状态；卸载和刷新时取消旧请求，不保留过期能力。 */
export function useRunningPlugins(token: string, revision = 0) {
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
