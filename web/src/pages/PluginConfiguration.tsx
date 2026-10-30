import { Alert, Button, Spin, Tabs } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import { ApiClient, errorMessage } from '../api';
import { BilibiliPlugin } from '../components/BilibiliPlugin';
import { ComfyLaunch } from '../components/ComfyLaunch';
import { NapCatPlugin } from '../components/NapCatPlugin';
import { StickerPlugin } from '../components/StickerPlugin';
import { isPluginRunning, pluginConfigurationLabels } from '../plugin-runtime';
import type { ConfigurablePlugin } from '../plugin-runtime';
import type { PluginInfo } from '../types';
import { Settings } from './Settings';

/** 独立配置页离开时只卸载账号组件；运行设置保留草稿，返回时重新核对状态。 */
export function PluginConfiguration({ token, pluginId, active = true }: {
  token: string; pluginId: ConfigurablePlugin; active?: boolean;
}) {
  const api = useMemo(() => new ApiClient(token), [token]);
  const [plugin, setPlugin] = useState<PluginInfo>();
  const [loading, setLoading] = useState(true);
  const [activationValidated, setActivationValidated] = useState(false);
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  const [stickerTab, setStickerTab] = useState('list');
  useEffect(() => {
    if (!active) { setActivationValidated(false); return; }
    const controller = new AbortController();
    // 刷新运行状态不能卸载同一插件的设置表单，否则会丢失未保存草稿。
    setLoading(true); setError(''); setActivationValidated(false);
    api.plugins(controller.signal).then(items => {
      if (!controller.signal.aborted) {
        setPlugin(items.find(item => item.id === pluginId)); setActivationValidated(true);
      }
    }).catch(failure => {
      if (!controller.signal.aborted) { setPlugin(undefined); setError(errorMessage(failure)); }
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false);
    });
    return () => controller.abort();
  }, [api, pluginId, revision, active]);

  // 身份变化时不沿用旧插件状态；普通刷新只有失败或停用才撤销原有配置能力。
  const currentPlugin = plugin?.id === pluginId ? plugin : undefined;
  return <div className="page-content" hidden={!active}>
    <div className="page-heading"><h1>{pluginConfigurationLabels[pluginId]}</h1>
      <div className="heading-actions">
        <Button loading={loading} onClick={() => setRevision(value => value + 1)}>刷新状态</Button></div>
    </div>
    {error && <Alert type="error" showIcon message={error} />}
    {loading && !currentPlugin ? <div className="loading"><Spin /></div>
      : error ? null : !currentPlugin || !isPluginRunning(currentPlugin)
        ? <Alert type="info" showIcon message="插件未运行，请在插件中心启用并手动重启后端。" />
        : pluginId === 'bilibili' ? active && activationValidated
          && <div className="settings-card"><BilibiliPlugin api={api} enabled /></div>
          : pluginId === 'stickers'
            ? <div hidden={!activationValidated}>
              {/* 配置面板首次打开才加载，之后保留草稿；离开列表卸载图库以取消媒体与上传请求。 */}
              <Tabs activeKey={stickerTab} onChange={setStickerTab} items={[
                { key: 'list', label: '列表', children: active && activationValidated
                  && stickerTab === 'list' && <StickerPlugin token={token} /> },
                { key: 'config', label: '配置', children: <Settings token={token}
                  categoryKeys={['stickers']} embedded
                  active={active && activationValidated && stickerTab === 'config'} /> },
              ]} />
            </div>
          : pluginId === 'comfy'
            ? <>{active && activationValidated && <div className="settings-card"><ComfyLaunch api={api} /></div>}
              {/* 状态面板随页面卸载取消轮询，设置表单独立保留未保存草稿。 */}
              <div hidden={!activationValidated}><Settings token={token} categoryKeys={['comfy']} embedded
                active={active && activationValidated} /></div></>
          : <>{active && activationValidated && <div className="settings-card"><NapCatPlugin api={api} /></div>}
            {/* 未验证本次运行状态时只隐藏表单，保留草稿且不允许操作旧缓存。 */}
            <div hidden={!activationValidated}><Settings token={token} categoryKeys={['qq', 'games']} embedded /></div></>}
  </div>;
}
