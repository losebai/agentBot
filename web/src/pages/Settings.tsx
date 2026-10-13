import { Alert, Button, Empty, Input, Spin, Switch, Tabs, Tag } from 'antd';
import { ReloadOutlined, SaveOutlined, SearchOutlined } from '@ant-design/icons';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { ApiClient, errorMessage } from '../api';
import type { JsonValue, SettingField, SettingsSnapshot } from '../types';

function initialValue(field: SettingField): string | boolean {
  // 密钥只显示是否已设置，不从快照构造掩码或实际值。
  if (field.kind === 'boolean') return field.value === true;
  if (field.value === null) return '';
  return field.kind === 'json' ? JSON.stringify(field.value, null, 2) : String(field.value);
}

function patchValue(field: SettingField, value: string | boolean): JsonValue {
  // 只转换被编辑的运行字段，完整约束与版本冲突由服务端校验。
  if (value === '' && field.nullable) return null;
  if (field.kind === 'boolean') return value === true;
  if (field.kind === 'number') {
    if (String(value).trim() === '' || !Number.isFinite(Number(value))) throw new Error(`${field.description}需要填写数字`);
    return Number(value);
  }
  if (field.kind === 'json') return JSON.parse(String(value)) as JsonValue;
  return String(value);
}

export function Settings({ token }: { token: string }) {
  // 分组展示启动配置和运行配置，草稿仅记录可在线修改字段。
  const api = useMemo(() => new ApiClient(token), [token]);
  const [snapshot, setSnapshot] = useState<SettingsSnapshot>();
  const [draft, setDraft] = useState<Record<string, string | boolean>>({});
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [filter, setFilter] = useState('');
  const [scope, setScope] = useState('runtime');
  const load = useCallback(async () => {
    setLoading(true); setError('');
    try { setSnapshot(await api.settings()); setDraft({}); setSaved(false); }
    catch (failure) { setError(errorMessage(failure)); }
    finally { setLoading(false); }
  }, [api]);
  useEffect(() => { void load(); }, [load]);

  async function save() {
    // 即使草稿含过期字段，也不能把静态参数提交为运行更新。
    if (!snapshot || saving) return;
    setError(''); setSaved(false); setSaving(true);
    try {
      const values: Record<string, JsonValue> = {};
      for (const field of snapshot.fields) {
        if (!field.read_only && field.scope !== 'static' && Object.hasOwn(draft, field.name)) {
          values[field.name] = patchValue(field, draft[field.name]);
        }
      }
      setSnapshot(await api.updateSettings(snapshot.database_revision, values));
      setDraft({}); setSaved(true);
    } catch (failure) { setError(errorMessage(failure)); }
    finally { setSaving(false); }
  }

  const fields = snapshot?.fields.filter(field => (field.scope || 'runtime') === scope
    && `${field.name} ${field.description}`.toLowerCase().includes(filter.toLowerCase())) || [];
  return <div className="page-content">
    <div className="page-heading"><div><p className="eyebrow">CONFIGURATION</p><h1>机器人配置</h1><p>调整聊天行为、模型和接入设置。</p></div>
      <div className="heading-actions"><Button icon={<ReloadOutlined />} onClick={() => void load()} loading={loading} disabled={saving}>
        {Object.keys(draft).length ? '放弃修改并重新加载' : '重新加载'}</Button>
        <Button type="primary" icon={<SaveOutlined aria-hidden />} loading={saving} disabled={!Object.keys(draft).length || loading} onClick={() => void save()}>保存修改</Button></div>
    </div>
    {error && <Alert message={error} type="error" showIcon className="notice" />}
    {saved && <Alert message="配置已保存；请查看应用版本与重启提示。" type="success" showIcon className="notice" />}
    {snapshot && <div className="version-strip"><span>数据库版本 {snapshot.database_revision} · 运行时版本 {snapshot.applied_revision}</span>
      <Tag color={snapshot.database_revision === snapshot.applied_revision && !snapshot.restart_required_fields.length ? 'green' : 'gold'}>
        {snapshot.restart_required_fields.length ? `需要重启：${snapshot.restart_required_fields.join(', ')}` : snapshot.database_revision !== snapshot.applied_revision ? '等待热刷新' : '已应用'}</Tag></div>}
    <Tabs activeKey={scope} onChange={setScope} items={[
      { key: 'runtime', label: '运行配置' }, { key: 'static', label: '静态配置' },
    ]} />
    {scope === 'static' && <Alert message="静态配置只读，请修改启动文件或环境变量后重启服务。" type="info" showIcon className="notice" />}
    <Input prefix={<SearchOutlined />} placeholder="搜索设置名称或描述" aria-label="搜索配置" value={filter} onChange={event => setFilter(event.target.value)} className="settings-search" />
    {loading ? <div className="loading"><Spin /></div> : <div className="settings-card">
      {!fields.length && <Empty description="没有可显示的配置" />}
      {fields.map(field => {
        const value = Object.hasOwn(draft, field.name) ? draft[field.name] : initialValue(field);
        const readOnly = field.read_only || field.scope === 'static';
        const change = (next: string | boolean) => {
          if (!readOnly) setDraft(previous => ({ ...previous, [field.name]: next }));
        };
        const id = `setting-${field.name}`;
        return <div className="setting-row" key={field.name}>
          <div className="setting-description"><label htmlFor={id}>{field.description}</label>{field.restart_required && <Tag>重启生效</Tag>}<code>{field.name}</code></div>
          <div className="setting-control">
            {field.kind === 'boolean' ? <Switch id={id} disabled={readOnly} checked={value === true} onChange={change} />
              : field.kind === 'secret' ? <Input.Password id={id} disabled={readOnly} value={String(value)} placeholder={field.secret_is_set ? readOnly ? '已设置' : '已设置 · 留空保持原值' : '未设置'} autoComplete="new-password" onChange={event => change(event.target.value)} />
              : field.kind === 'json' || (field.kind === 'string' && String(value).length > 160) ? <Input.TextArea id={id} disabled={readOnly} value={String(value)} autoSize={{ minRows: 2, maxRows: 8 }} onChange={event => change(event.target.value)} />
              : <Input id={id} disabled={readOnly} type={field.kind === 'number' ? 'number' : 'text'} step="any" value={String(value)} onChange={event => change(event.target.value)} />}
            {!readOnly && field.kind === 'secret' && field.nullable && field.secret_is_set && <Button size="small" type="text" danger onClick={() => change('')}>清除此密钥</Button>}
          </div>
        </div>;
      })}
    </div>}
  </div>;
}
