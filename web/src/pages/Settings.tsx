import { Alert, Button, Empty, Input, Spin, Switch, Tabs, Tag } from 'antd';
import { ReloadOutlined, SaveOutlined, SearchOutlined } from '@ant-design/icons';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { ApiClient, errorMessage } from '../api';
import type { JsonValue, SettingField, SettingsSnapshot } from '../types';
import { ComfyWorkflows } from './ComfyWorkflows';
import { QueueMaintenance } from './QueueMaintenance';
import { modelSettingNames, SettingsModelSelect, useSettingsModels } from '../components/SettingsModelSelect';

// 分类仅改变展示，共享同一草稿与版本；按字段用途归类，不用描述文本猜测。
const categories = [
  { key: 'basic', label: '基础设置', description: '服务资源、会话与其他基础配置。' },
  { key: 'llm', label: 'LLM设置', description: '模型、Agent、推理、图片理解与生成、提示词和记忆。' },
  { key: 'qq', label: 'QQ', description: 'OneBot 接入、机器人名称、账号与访问权限。' },
  { key: 'reply', label: '自动回复设置', description: '关键词回复、回复限制、群聊上下文、主动发言与定时任务。' },
  { key: 'games', label: '游戏资讯设置', description: '永劫无间账号凭据、服务器、赛季与查询限制。' },
  { key: 'comfy', label: 'Comfy', description: '本地 ComfyUI 连接和工作流。' },
] as const;
type Category = typeof categories[number]['key'];
// null 仅表示显式清除可空密钥，空字符串仍是表单输入或静态密钥的保留操作。
type DraftValue = string | boolean | null;
const llmSettingNames = new Set([
  'chat_model', 'chat_vision_model', 'chat_image_model', 'chat_system_prompt', 'chat_mcp_services',
  'chat_image_recognition_enabled', 'chat_max_images_per_message', 'chat_tagged_memory_enabled',
]);
const qqSettingNames = new Set([
  'nickname', 'superusers', 'command_start', 'command_sep', 'api_timeout',
  'session_expire_timeout', 'chat_allow_all', 'chat_mention_tool_enabled',
]);
const replySettingNames = new Set([
  'chat_priority', 'chat_reset_priority', 'chat_keyword_at_replies',
  'chat_cooldown_seconds', 'chat_chunk_chars', 'chat_max_reply_chars',
]);
// 兼容旧服务端快照：菜单开关和内部搜索绑定均不能作为设置字段编辑或提交。
const hiddenSettingNames = new Set(['app_enabled_plugins', 'chat_python_search_mcp_code']);

/** 按用途分类；供应商模型归 LLM，避免生图模型因图片字段前缀混入 Comfy。 */
function fieldCategory(field: SettingField): Category {
  const name = field.name;
  if (name.startsWith('chat_naraka_')) return 'games';
  if (replySettingNames.has(name) || [
    'chat_auto_', 'chat_group_', 'chat_user_message_', 'chat_job_',
  ].some(prefix => name.startsWith(prefix))) return 'reply';
  if (name.startsWith('agent_bot_napcat_') || qqSettingNames.has(name) || [
    'onebot_', 'chat_allowed_', 'chat_blocked_',
  ].some(prefix => name.startsWith(prefix))) return 'qq';
  if (llmSettingNames.has(name) || [
    'chat_llm_', 'chat_python_', 'chat_agent_', 'chat_web_search_',
    'chat_memory_', 'chat_history_', 'chat_tts_', 'chat_image_',
  ].some(prefix => name.startsWith(prefix))) return 'llm';
  // 生图模型和预算与 Comfy 无关，只有本地工作流字段归入 Comfy。
  if (name.startsWith('chat_local_workflow_')) return 'comfy';
  return 'basic';
}

/** 结构值使用 JSON 编辑器，兼容旧接口对泛型类型的误判；密钥保留脱敏控件。 */
function settingKind(field: SettingField): SettingField['kind'] {
  if (field.kind !== 'secret' && field.value !== null && typeof field.value === 'object') return 'json';
  return field.kind;
}

/** 静态权限以服务端明确授权为准，缺少授权信息时不能编辑启动文件。 */
function isReadOnly(field: SettingField): boolean {
  if (field.scope === 'static') return field.read_only !== false;
  return field.read_only === true;
}

/** 格式化对象与数组，保留可空参数及机器人名称的逐行编辑方式。 */
function initialValue(field: SettingField): string | boolean {
  // 空布尔值表示不传该参数，不能折叠为 false；密钥不回填实际内容。
  if (field.value === null) return '';
  if (settingKind(field) === 'boolean') return field.value === true;
  // 名称列表按行展示，管理员无需编辑 JSON。
  if (field.name === 'nickname') return (field.value as string[]).join('\n');
  return settingKind(field) === 'json' ? JSON.stringify(field.value, null, 2) : String(field.value);
}

/** 根据同一展示类型解析草稿，结构字段提交 JSON 值，完整约束仍由服务端校验。 */
function patchValue(field: SettingField, value: DraftValue): JsonValue {
  // 只转换被编辑的运行字段，完整约束与版本冲突由服务端校验。
  if (value === null || (value === '' && field.nullable)) return null;
  const kind = settingKind(field);
  if (kind === 'boolean') return value === true;
  if (field.name === 'nickname') {
    // 留空明确关闭名称触发；名称在后端统一去重并随运行快照生效。
    return String(value).split(/\r?\n/).map(name => name.trim()).filter(Boolean);
  }
  if (kind === 'number') {
    if (String(value).trim() === '' || !Number.isFinite(Number(value))) throw new Error(`${field.description}需要填写数字`);
    return Number(value);
  }
  if (kind === 'json') return JSON.parse(String(value)) as JsonValue;
  return String(value);
}

/** 按用途管理机器人配置，跨分类保留草稿并共用版本校验与保存操作。 */
export function Settings({ token, onPlugins }: { token: string; onPlugins?: () => void }) {
  const api = useMemo(() => new ApiClient(token), [token]);
  const [snapshot, setSnapshot] = useState<SettingsSnapshot>();
  const [draft, setDraft] = useState<Record<string, DraftValue>>({});
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [filter, setFilter] = useState('');
  const [category, setCategory] = useState<Category>('basic');
  const [comfyTab, setComfyTab] = useState('connection');
  const [workflowBusy, setWorkflowBusy] = useState(false);
  const [settingsRefreshing, setSettingsRefreshing] = useState(false);
  const showModels = category === 'llm' && !!snapshot?.fields.some(field => modelSettingNames.has(field.name));
  const models = useSettingsModels(token, showModels);
  const load = useCallback(async () => {
    setLoading(true); setError('');
    try { setSnapshot(await api.settings()); setDraft({}); setSaved(false); }
    catch (failure) { setError(errorMessage(failure)); }
    finally { setLoading(false); }
  }, [api]);
  useEffect(() => { void load(); }, [load]);

  /** 默认工作流已单独保存：刷新配置版本，移除旧默认键草稿并保留其他修改。 */
  const workflowSettingsChanged = useCallback(async () => {
    setSettingsRefreshing(true); setSaved(false);
    setDraft(previous => {
      const updated = { ...previous };
      delete updated.chat_local_workflow_default_key;
      return updated;
    });
    try { setSnapshot(await api.settings()); }
    finally { setSettingsRefreshing(false); }
  }, [api]);

  /** 动态草稿写入数据库，静态草稿带启动文件版本保存，等待用户手动重启。 */
  async function save() {
    // 仅提交实际编辑且允许修改的字段，不将完整快照中的陈旧静态值写回文件。
    if (!snapshot || saving || workflowBusy || settingsRefreshing) return;
    setError(''); setSaved(false); setSaving(true);
    try {
      const values: Record<string, JsonValue> = {};
      let startupChanged = false;
      for (const field of snapshot.fields) {
        if (!hiddenSettingNames.has(field.name) && !isReadOnly(field) && Object.hasOwn(draft, field.name)) {
          values[field.name] = patchValue(field, draft[field.name]);
          if (field.scope === 'static') startupChanged = true;
        }
      }
      setSnapshot(await api.updateSettings(snapshot.database_revision, values,
        startupChanged ? snapshot.startup_revision : undefined));
      setDraft({}); setSaved(true);
    } catch (failure) { setError(errorMessage(failure)); }
    finally { setSaving(false); }
  }

  const fields = snapshot?.fields.filter(field => !hiddenSettingNames.has(field.name) && fieldCategory(field) === category
    && `${field.name} ${field.description}`.toLowerCase().includes(filter.toLowerCase()))
    // 名称是群聊触发入口，在 QQ 分类中优先展示。
    .sort((left, right) => Number(right.name === 'nickname') - Number(left.name === 'nickname')) || [];
  return <div className="page-content">
    <div className="page-heading"><div><p className="eyebrow">CONFIGURATION</p>
      <h1>机器人配置</h1>
      <p>按用途调整配置，切换分类保留草稿，保存时统一提交所有修改。</p></div>
      <div className="heading-actions"><Button icon={<ReloadOutlined />} onClick={() => void load()} loading={loading} disabled={saving || workflowBusy || settingsRefreshing}>
        {Object.keys(draft).length ? '放弃修改并重新加载' : '重新加载'}</Button>
        <Button type="primary" icon={<SaveOutlined aria-hidden />} loading={saving} disabled={!Object.keys(draft).length || loading || workflowBusy || settingsRefreshing} onClick={() => void save()}>保存修改</Button></div>
    </div>
    {error && <Alert message={error} type="error" showIcon className="notice" />}
    {saved && <Alert message="配置已保存；请查看应用版本与重启提示。" type="success" showIcon className="notice" />}
    {snapshot && <div className="version-strip"><span>数据库版本 {snapshot.database_revision} · 运行时版本 {snapshot.applied_revision}</span>
      <Tag color={snapshot.database_revision === snapshot.applied_revision && !snapshot.restart_required_fields.length ? 'green' : 'gold'}>
        {snapshot.restart_required_fields.length ? `需要重启：${snapshot.restart_required_fields.join(', ')}` : snapshot.database_revision !== snapshot.applied_revision ? '等待热刷新' : '已应用'}</Tag></div>}
    <div className="settings-layout">
      <nav className="settings-navigation" aria-label="机器人配置分类">
        {categories.map(item => <button key={item.key} type="button"
          disabled={workflowBusy || settingsRefreshing}
          className={item.key === category ? 'active' : ''}
          aria-current={item.key === category ? 'page' : undefined}
          onClick={() => setCategory(item.key)}>{item.label}</button>)}
      </nav>
      <section className="settings-category" aria-label={categories.find(item => item.key === category)?.label}>
        <div className="settings-category-heading">
          <h2>{categories.find(item => item.key === category)?.label}</h2>
          <p>{categories.find(item => item.key === category)?.description}</p>
        </div>
        {category === 'comfy' && <Tabs activeKey={comfyTab} onChange={setComfyTab} items={[
          { key: 'connection', label: '连接设置', disabled: workflowBusy || settingsRefreshing },
          { key: 'workflows', label: '工作流管理', disabled: loading || saving },
        ]} />}
        {category === 'comfy' && comfyTab === 'workflows'
          ? <ComfyWorkflows token={token} disabled={loading || saving || settingsRefreshing}
            onSettingsChanged={workflowSettingsChanged} onBusyChange={setWorkflowBusy} />
          : <>
    {category === 'basic' && onPlugins && <Button type="link" onClick={onPlugins}>管理内置插件</Button>}
    {category === 'basic' && <QueueMaintenance token={token} />}
    {fields.some(field => field.scope === 'static') && <Alert message="启动配置保存后需手动“服务→重启后端”生效；环境变量覆盖或缺少启动文件的字段只读。" type="info" showIcon className="notice" />}
    <Input prefix={<SearchOutlined />} placeholder="搜索设置名称或描述" aria-label="搜索配置" value={filter} onChange={event => setFilter(event.target.value)} className="settings-search" />
    {showModels && <>
      {models.error && <Alert message={`模型列表读取失败：${models.error}`} type="error" showIcon className="notice" />}
      <div className="heading-actions">
        <Button loading={models.busy} onClick={() => void models.load()}>刷新模型列表</Button>
        {models.page.next_cursor && <Button disabled={models.busy}
          onClick={() => void models.load(models.page.next_cursor!)}>加载更多模型</Button>}
      </div>
    </>}
    {loading ? <div className="loading"><Spin /></div> : <div className="settings-card">
      {!fields.length && <Empty description="没有可显示的配置" />}
      {fields.map(field => {
        const kind = settingKind(field);
        const value = Object.hasOwn(draft, field.name) ? draft[field.name] ?? '' : initialValue(field);
        const readOnly = isReadOnly(field);
        // 静态密钥手动留空保留原值；显式清除按钮使用 null，避免两种操作混淆。
        const change = (next: DraftValue) => {
          if (!readOnly) setDraft(previous => {
            const updated = { ...previous };
            if (field.scope === 'static' && kind === 'secret' && next === '') delete updated[field.name];
            else updated[field.name] = next;
            return updated;
          });
        };
        const id = `setting-${field.name}`;
        return <div className="setting-row" key={field.name}>
          <div className="setting-description"><label htmlFor={id}>{field.description}</label>{field.restart_required && <Tag>重启生效</Tag>}<code>{field.name}</code></div>
          <div className="setting-control">
            {modelSettingNames.has(field.name) ? <SettingsModelSelect id={id} value={String(value)}
              readOnly={readOnly} nullable={field.nullable} directory={models} onChange={change} />
              : field.name === 'nickname' ? <Input.TextArea id={id} disabled={readOnly}
              value={String(value)} placeholder="每行填写一个名称或别名，如：桃桃"
              autoSize={{minRows: 2, maxRows: 6}} onChange={event => change(event.target.value)} />
              : kind === 'boolean' && field.nullable ? <select id={id} disabled={readOnly}
              value={String(value)} onChange={event => change(event.target.value === '' ? '' : event.target.value === 'true')}>
              <option value="">未设置（不传此参数）</option><option value="true">启用</option><option value="false">禁用</option>
            </select>
              : kind === 'boolean' ? <Switch id={id} disabled={readOnly} checked={value === true} onChange={change} />
              : kind === 'secret' ? <Input.Password id={id} disabled={readOnly} value={String(value)} placeholder={field.secret_is_set ? readOnly ? '已设置' : '已设置 · 留空保持原值' : '未设置'} autoComplete="new-password" onChange={event => change(event.target.value)} />
              : kind === 'json' || (kind === 'string' && String(value).length > 160) ? <Input.TextArea id={id} disabled={readOnly} value={String(value)} autoSize={{ minRows: 2, maxRows: 8 }} onChange={event => change(event.target.value)} />
              : <Input id={id} disabled={readOnly} type={kind === 'number' ? 'number' : 'text'} step="any" value={String(value)} onChange={event => change(event.target.value)} />}
            {field.name === 'nickname' && <small>每行一个名称或别名，保存后热更新；留空关闭名字触发。</small>}
            {field.name === 'onebot_v11_access_token' && <small>{readOnly
              ? '当前字段由启动环境覆盖或缺少启动文件，请检查启动配置；与 NapCat Token 保持一致。'
              : '静态启动配置，可在这里保存；手动“服务→重启后端”后生效，请与 NapCat Token 保持一致。留空不修改。'}</small>}
            {field.scope === 'static' && readOnly && field.name !== 'onebot_v11_access_token' && <small>此字段由启动环境提供或缺少启动文件，请检查启动配置。</small>}
            {field.name === 'chat_session_secret' && <small>修改并重启后会使用新的会话与身份命名空间；旧历史和记忆数据保留，供应商与 MCP 密钥不受影响。留空不修改。</small>}
            {field.scope === 'static' && draft[field.name] === null && <small>已标记清除此密钥，保存后重启生效。</small>}
            {!readOnly && field.kind === 'secret' && field.nullable && field.secret_is_set && <Button size="small" type="text" danger onClick={() => change(field.scope === 'static' ? null : '')}>清除此密钥</Button>}
          </div>
        </div>;
      })}
    </div>}
          </>}
      </section>
    </div>
  </div>;
}
