import { Alert, Button, Empty, Spin, Switch, Tag } from 'antd';
import { ReloadOutlined, SaveOutlined } from '@ant-design/icons';
import { useEffect, useMemo, useRef, useState } from 'react';
import { ApiClient, errorMessage } from '../api';
import type { PluginInfo, SettingsSnapshot } from '../types';

const statusLabels: Record<PluginInfo['status'], string> = {
  disabled: '未启用', prepared: '已准备', running: '运行中', stopped: '已停止', failed: '启动失败',
};

/** 内置插件只保存启动选择，网页不提供进程重启或动态卸载能力。 */
export function Plugins({ token }: { token: string }) {
  const api = useMemo(() => new ApiClient(token), [token]);
  const [plugins, setPlugins] = useState<PluginInfo[]>([]);
  const [snapshot, setSnapshot] = useState<SettingsSnapshot | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState('');
  const controller = useRef<AbortController | null>(null);
  const active = useRef(true);

  /** 刷新会明确放弃草稿，每次只允许最新一次读取改变页面。 */
  async function load() {
    controller.current?.abort();
    const current = new AbortController();
    controller.current = current;
    setLoading(true); setError(''); setSaved(false);
    try {
      const [items, settings] = await Promise.all([api.plugins(current.signal), api.settings(current.signal)]);
      if (current.signal.aborted || !active.current) return;
      setPlugins(items); setSnapshot(settings);
      setSelected(items.filter(item => item.saved_enabled).map(item => item.id));
    } catch (failure) {
      if (!current.signal.aborted && active.current) setError(errorMessage(failure));
    } finally {
      if (!current.signal.aborted && active.current) setLoading(false);
    }
  }

  useEffect(() => {
    active.current = true;
    void load();
    // 卸载与 StrictMode 重建均取消旧读取；已提交保存仍由后端完成。
    return () => { active.current = false; controller.current?.abort(); };
  }, [api]);

  const changed = plugins.some(item => selected.includes(item.id) !== item.saved_enabled);
  const pendingRestart = plugins.some(item => item.restart_required);

  /** 保存只提交列表和数据库版本，失败保留草稿，不修改当前插件状态。 */
  async function save() {
    if (!snapshot || saving || !changed) return;
    setSaving(true); setError(''); setSaved(false);
    try {
      const settings = await api.updateSettings(snapshot.database_revision, { app_enabled_plugins: selected });
      if (!active.current) return;
      setSnapshot(settings); setSaved(true);
      const items = await api.plugins();
      if (active.current) setPlugins(items);
    } catch (failure) {
      if (active.current) setError(errorMessage(failure));
    } finally {
      if (active.current) setSaving(false);
    }
  }

  return <div className="page-content">
    <div className="page-heading"><div><p className="eyebrow">PLUGINS</p><h1>插件管理</h1>
      <p>选择启动时加载的内置插件，保存后手动重启后端生效。</p></div>
      <div className="heading-actions">
        <Button icon={<ReloadOutlined />} disabled={saving} loading={loading} onClick={() => void load()}>
          {changed ? '放弃修改并重新加载' : '重新加载'}</Button>
        <Button type="primary" icon={<SaveOutlined />} loading={saving} disabled={loading || !changed}
          onClick={() => void save()}>保存</Button>
      </div></div>
    {error && <Alert message={error} type="error" showIcon className="notice" />}
    {saved && <Alert message="插件选择已保存；当前进程保持原状态，请手动重启后端。" type="success" showIcon className="notice" />}
    {pendingRestart && <Alert message="重启后生效" description="使用已有的“服务 → 重启后端”，或手动重启服务。" type="warning" showIcon className="notice" />}
    {loading ? <div className="loading"><Spin /></div> : <div className="settings-card">
      {!plugins.length && <Empty description="没有内置插件" />}
      {plugins.map(plugin => <div className="setting-row" key={plugin.id}>
        <div className="setting-description"><label htmlFor={`plugin-${plugin.id}`}>{plugin.name}</label>
          <Tag>{statusLabels[plugin.status]}</Tag><code>{plugin.id} · {plugin.version}</code>
          <p>{plugin.description}</p></div>
        <div className="setting-control"><Switch id={`plugin-${plugin.id}`} aria-label={`启用 ${plugin.name}`}
          disabled={saving} checked={selected.includes(plugin.id)} onChange={enabled => {
            setSaved(false);
            setSelected(previous => enabled ? [...previous, plugin.id] : previous.filter(id => id !== plugin.id));
          }} />
          <small>当前进程：{plugin.enabled ? '已启用' : '未启用'}；已保存：{plugin.saved_enabled ? '启用' : '停用'}</small>
        </div></div>)}
    </div>}
  </div>;
}
