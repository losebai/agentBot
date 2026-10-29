import { ChoiceSelect } from '../components/ChoiceSelect';
import { Alert, Button, Empty, Input, Spin, Switch, Tabs, Tag } from 'antd';
import { ReloadOutlined, SaveOutlined, SearchOutlined } from '@ant-design/icons';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { ApiClient, errorMessage } from '../api';
import type { JsonValue, SettingField, SettingsSnapshot } from '../types';
import { ComfyWorkflows } from './ComfyWorkflows';
import { QueueMaintenance } from './QueueMaintenance';
import { modelSettingNames, SettingsModelSelect, useSettingsModels } from '../components/SettingsModelSelect';
import { useRunningPlugins } from '../plugin-runtime';

// 分类仅改变展示，共享同一草稿与版本；按字段用途归类，不用描述文本猜测。
const categories = [
  { key: 'basic', label: '基础设置', description: '服务资源、会话与其他基础配置。' },
  { key: 'llm', label: 'LLM设置', description: '模型、Agent、推理、图片理解与生成和提示词。' },
  { key: 'memory', label: '记忆设置', description: '会话心智记忆、标签记忆、历史保留、自动压缩和向量检索。' },
  { key: 'qq', label: 'QQ', description: 'OneBot 接入、机器人名称、账号与访问权限。' },
  { key: 'reply', label: '自动回复设置', description: '关键词回复、回复限制、群聊上下文、主动发言与定时任务。' },
  { key: 'games', label: '游戏资讯设置', description: '永劫无间账号凭据、服务器、赛季与查询限制。' },
  { key: 'comfy', label: 'Comfy', description: '本地 ComfyUI 连接和工作流。' },
  { key: 'stickers', label: '表情包设置', description: '图库识图模型、自定义识图要求与语境匹配超时。' },
] as const;
type Category = typeof categories[number]['key'];
// null 仅表示显式清除可空密钥，空字符串仍是表单输入或静态密钥的保留操作。
type DraftValue = string | boolean | null;
const llmSettingNames = new Set([
  'chat_model', 'chat_vision_model', 'chat_image_model', 'chat_system_prompt', 'chat_mcp_services',
  'chat_image_recognition_enabled', 'chat_max_images_per_message',
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
const hiddenSettingNames = new Set(['app_enabled_plugins', 'chat_web_search_max_steps']);

/** 仅展示当前决策方式的依赖，切换方式时不删除另一方式的草稿或已存值。 */
function visibleDecisionField(name: string, backend: string): boolean {
  if (['chat_auto_speak_mcp_code', 'chat_auto_speak_laya_prompt'].includes(name)) return backend === 'laya';
  if (['chat_auto_speak_decision_model', 'chat_auto_speak_llm_prompt'].includes(name)) return backend === 'llm';
  return true;
}

/** 仅展示当前触发模式的时间配置，隐藏字段仍保留已有草稿供切换和保存。 */
function visibleTriggerField(name: string, mode: string): boolean {
  if (['chat_auto_speak_interval_seconds', 'chat_auto_speak_initial_delay_seconds'].includes(name)) return mode === 'interval';
  if (name === 'chat_auto_speak_message_interval_seconds') return mode === 'message';
  return true;
}

/** 按用途分类；记忆及历史独立归类，记忆向量模型也遵循该分类。 */
function fieldCategory(field: SettingField): Category {
  const name = field.name;
  if (name === 'chat_tagged_memory_enabled'
    || ['chat_memory_', 'chat_history_'].some(prefix => name.startsWith(prefix))) return 'memory';
  if (name.startsWith('chat_naraka_')) return 'games';
  if (name.startsWith('chat_sticker_')) return 'stickers';
  if (replySettingNames.has(name) || [
    'chat_auto_', 'chat_group_', 'chat_user_message_', 'chat_job_', 'chat_multi_reply_',
  ].some(prefix => name.startsWith(prefix))) return 'reply';
  if (name.startsWith('agent_bot_napcat_') || qqSettingNames.has(name) || [
    'onebot_', 'chat_allowed_', 'chat_blocked_',
  ].some(prefix => name.startsWith(prefix))) return 'qq';
  if (llmSettingNames.has(name) || [
    'chat_llm_', 'chat_python_', 'chat_agent_', 'chat_web_search_',
    'chat_tts_', 'chat_image_',
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

/** 按页面分类编辑配置；嵌入插件页时只保留操作栏，避免重复页面与分类标题。 */
export function Settings({ token, onPlugins, categoryKeys, embedded = false, title = '机器人配置', description, active = true }: {
  token: string; onPlugins?: () => void; categoryKeys?: readonly Category[];
  embedded?: boolean; title?: string; description?: string; active?: boolean;
}) {
  const api = useMemo(() => new ApiClient(token), [token]);
  const [snapshot, setSnapshot] = useState<SettingsSnapshot>();
  const [draft, setDraft] = useState<Record<string, DraftValue>>({});
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [filter, setFilter] = useState('');
  const [category, setCategory] = useState<Category>(categoryKeys?.[0] || 'basic');
  const [pluginsRevision, setPluginsRevision] = useState(0);
  const { runningPlugins, pluginsError } = useRunningPlugins(token, pluginsRevision);
  const qqRunning = runningPlugins.has('qqbot');
  // 插件配置跟随实际进程状态；已保存启用选择不能授予图库管理能力。
  const visibleCategories = categories.filter(item => (!categoryKeys || categoryKeys.includes(item.key))
    && (qqRunning || !['qq', 'games'].includes(item.key))
    && (item.key !== 'stickers' || runningPlugins.has('stickers')));
  const currentCategory = visibleCategories.some(item => item.key === category) ? category : visibleCategories[0]?.key;
  const [comfyTab, setComfyTab] = useState('connection');
  const [workflowBusy, setWorkflowBusy] = useState(false);
  const [settingsRefreshing, setSettingsRefreshing] = useState(false);
  const decisionBackend = String(draft.chat_auto_speak_decision_backend
    ?? snapshot?.fields.find(field => field.name === 'chat_auto_speak_decision_backend')?.value ?? 'laya');
  // 优先响应未保存的模式选择；旧快照缺少该字段时沿用定时模式，不生成草稿。
  const triggerMode = String(draft.chat_auto_speak_trigger_mode
    ?? snapshot?.fields.find(field => field.name === 'chat_auto_speak_trigger_mode')?.value ?? 'interval');
  const showModels = !!snapshot?.fields.some(field => modelSettingNames.has(field.name)
    && fieldCategory(field) === currentCategory && visibleDecisionField(field.name, decisionBackend));
  const models = useSettingsModels(token, showModels);
  const load = useCallback(async (refreshPlugins = true) => {
    if (refreshPlugins) setPluginsRevision(previous => previous + 1);
    setLoading(true); setError('');
    try { setSnapshot(await api.settings()); setDraft({}); setSaved(false); }
    catch (failure) { setError(errorMessage(failure)); }
    finally { setLoading(false); }
  }, [api]);
  useEffect(() => { void load(false); }, [load]);

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
        if ((!categoryKeys || categoryKeys.includes(fieldCategory(field)))
          && !hiddenSettingNames.has(field.name) && !isReadOnly(field) && Object.hasOwn(draft, field.name)) {
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

  const fields = snapshot?.fields.filter(field => !hiddenSettingNames.has(field.name) && fieldCategory(field) === currentCategory
    && visibleDecisionField(field.name, decisionBackend)
    && visibleTriggerField(field.name, triggerMode)
    && `${field.name} ${field.description}`.toLowerCase().includes(filter.toLowerCase()))
    // 名称是群聊触发入口，在 QQ 分类中优先展示。
    .sort((left, right) => Number(right.name === 'nickname') - Number(left.name === 'nickname')) || [];
  return <div className={embedded ? 'plugin-settings' : 'page-content'}>
    <div className={embedded ? 'settings-toolbar' : 'page-heading'}>
      {!embedded && <div><p className="eyebrow">CONFIGURATION</p>
        <h1>{title}</h1>
        <p>{description || '按用途调整配置，切换分类保留草稿，保存时统一提交所有修改。'}</p></div>}
      <div className="heading-actions"><Button icon={<ReloadOutlined />} onClick={() => void load()} loading={loading} disabled={saving || workflowBusy || settingsRefreshing}>
        {Object.keys(draft).length ? '放弃修改并重新加载' : '重新加载'}</Button>
        <Button type="primary" icon={<SaveOutlined aria-hidden />} loading={saving} disabled={!Object.keys(draft).length || loading || workflowBusy || settingsRefreshing} onClick={() => void save()}>保存修改</Button></div>
    </div>
    {error && <Alert message={error} type="error" showIcon className="notice" />}
    {pluginsError && <Alert message={'插件状态读取失败：' + pluginsError} type="error" showIcon className="notice" />}
    {saved && <Alert message="配置已保存；请查看应用版本与重启提示。" type="success" showIcon className="notice" />}
    {snapshot && <div className="version-strip"><span>数据库版本 {snapshot.database_revision} · 运行时版本 {snapshot.applied_revision}</span>
      <Tag color={snapshot.database_revision === snapshot.applied_revision && !snapshot.restart_required_fields.length ? 'green' : 'gold'}>
        {snapshot.restart_required_fields.length ? `需要重启：${snapshot.restart_required_fields.join(', ')}` : snapshot.database_revision !== snapshot.applied_revision ? '等待热刷新' : '已应用'}</Tag></div>}
    <div className={`settings-layout${embedded && visibleCategories.length <= 1 ? ' settings-layout-single' : ''}`}>
      {(!embedded || visibleCategories.length > 1) && <nav className="settings-navigation" aria-label="机器人配置分类">
        {visibleCategories.map(item => <button key={item.key} type="button"
          disabled={workflowBusy || settingsRefreshing}
          className={item.key === currentCategory ? 'active' : ''}
          aria-current={item.key === currentCategory ? 'page' : undefined}
          onClick={() => setCategory(item.key)}>{item.label}</button>)}
      </nav>}
      <section className="settings-category" aria-label={categories.find(item => item.key === currentCategory)?.label}>
        {!embedded && <div className="settings-category-heading">
          <h2>{categories.find(item => item.key === currentCategory)?.label}</h2>
          <p>{categories.find(item => item.key === currentCategory)?.description}</p>
        </div>}
        {currentCategory === 'comfy' && <Tabs activeKey={comfyTab} onChange={setComfyTab} items={[
          { key: 'connection', label: '连接设置', disabled: workflowBusy || settingsRefreshing },
          { key: 'workflows', label: '工作流管理', disabled: loading || saving },
        ]} />}
        {currentCategory === 'comfy' && comfyTab === 'workflows'
          // 离开插件页取消工作流读取，连接表单仍挂载以保留未保存草稿。
          ? active && <ComfyWorkflows token={token} disabled={loading || saving || settingsRefreshing}
            onSettingsChanged={workflowSettingsChanged} onBusyChange={setWorkflowBusy} />
          : <>
    {currentCategory === 'basic' && onPlugins && <Button type="link" onClick={onPlugins}>管理内置插件</Button>}
    {qqRunning && (categoryKeys ? categoryKeys.includes('qq') && currentCategory === 'qq'
      : currentCategory === 'basic') && <QueueMaintenance token={token} />}
    {fields.some(field => field.scope === 'static') && <Alert message="启动配置保存后需手动“服务→重启后端”生效；环境变量覆盖或缺少启动文件的字段只读。" type="info" showIcon className="notice" />}
    {currentCategory === 'reply' && fields.some(field => field.name === 'chat_multi_reply_enabled') && <Alert
      message="自动多条回复开启后，由模型按语义决定消息边界，短回答可只发送一条；最多条数、发送间隔和分条提示词可在下方调整。"
      type="info" showIcon className="notice" />}
    <Input prefix={<SearchOutlined />} placeholder="搜索设置名称或描述" aria-label="搜索配置" value={filter} onChange={event => setFilter(event.target.value)} className="settings-search" />
    {showModels && <>
      {models.error && <Alert message={`模型列表读取失败：${models.error}`} type="error" showIcon className="notice" />}
      <div className="heading-actions">
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
        // 可选模型用空字符串表示未配置，清空选择不会把字符串字段改成 null。
        const modelCanClear = field.nullable || ['chat_memory_embedding_model', 'chat_sticker_model'].includes(field.name);
        return <div className="setting-row" key={field.name}>
          <div className="setting-description"><label htmlFor={id}>{field.description}</label>{field.restart_required && <Tag>重启生效</Tag>}<code>{field.name}</code></div>
          <div className="setting-control">
            {field.name === 'chat_auto_speak_decision_backend' ? <ChoiceSelect id={id} disabled={readOnly}
              value={String(value)} onChange={change}>
              <option value="laya">Laya</option><option value="llm">LLM</option>
            </ChoiceSelect>
              : field.name === 'chat_auto_speak_trigger_mode' ? <ChoiceSelect id={id} disabled={readOnly}
              value={String(value)} onChange={change}>
              <option value="interval">定时模式</option><option value="message">消息模式</option>
            </ChoiceSelect>
              : modelSettingNames.has(field.name) ? <SettingsModelSelect id={id} value={String(value)}
              readOnly={readOnly} nullable={modelCanClear}
              directory={models} onChange={change} vision={field.name === 'chat_sticker_model'} />
              : field.name === 'nickname' ? <Input.TextArea id={id} disabled={readOnly}
              value={String(value)} placeholder="每行填写一个名称或别名，如：桃桃"
              autoSize={{minRows: 2, maxRows: 6}} onChange={event => change(event.target.value)} />
              : kind === 'boolean' && field.nullable ? <ChoiceSelect id={id} disabled={readOnly}
              value={String(value)} onChange={event => change(event === '' ? '' : event === 'true')}>
              <option value="">未设置（不传此参数）</option><option value="true">启用</option><option value="false">禁用</option>
            </ChoiceSelect>
              : kind === 'boolean' ? <Switch id={id} disabled={readOnly} checked={value === true} onChange={change} />
              : kind === 'secret' ? <Input.Password id={id} disabled={readOnly} value={String(value)} placeholder={field.secret_is_set ? readOnly ? '已设置' : '已设置 · 留空保持原值' : '未设置'} autoComplete="new-password" onChange={event => change(event.target.value)} />
              : kind === 'json' || ['chat_auto_speak_llm_prompt', 'chat_auto_speak_prompt', 'chat_multi_reply_prompt', 'chat_sticker_vision_prompt'].includes(field.name) || (kind === 'string' && String(value).length > 160) ? <Input.TextArea id={id} disabled={readOnly} value={String(value)} autoSize={{ minRows: 2, maxRows: 8 }} onChange={event => change(event.target.value)} />
              : <Input id={id} disabled={readOnly} type={kind === 'number' ? 'number' : 'text'} step="any" value={String(value)} onChange={event => change(event.target.value)} />}
            {field.name === 'nickname' && <small>每行一个名称或别名，保存后热更新；留空关闭名字触发。</small>}
            {field.name === 'chat_sticker_model' && <small>独立选择已启用且开启视觉能力的模型，用于图片说明和文字语境匹配。留空可先维护图库，手动填写说明。</small>}
            {field.name === 'chat_sticker_vision_prompt' && <small>可补充希望识别的细节或表达风格；留空使用默认识图要求，自定义标签不会被识图覆盖。</small>}
            {field.name === 'superusers' && <small>填写主人 QQ 号的 JSON 数组，如 ["123456789"]；保存后手动重启后端生效。仅主人可使用配置查询和修改工具，留空数组不授权任何人；密钥请在管理页面编辑。</small>}
            {field.name === 'chat_agent_max_steps' && <small>按实际工具调用次数计数，所有 Agent 和联网请求共用。默认 5，可填写任意正整数；留空不限次数，仍遵守请求超时。</small>}
            {field.name === 'chat_auto_speak_llm_prompt' && <small>使用独立决策模型和此提示词，结合当前收到的消息及近期群聊进行单次普通调用；不读取会话记忆、历史轮次或 Agent 人设。仅返回 {`{"choice":"speak"}`} 或 {`{"choice":"silent"}`}。</small>}
            {field.name === 'chat_auto_speak_trigger_mode' && <small>定时模式按周期检查；消息模式由群消息触发检查。两种模式分别保留时间配置。</small>}
            {field.name === 'chat_auto_speak_interval_seconds' && <small>仅定时模式使用，设置周期检查的间隔秒数。</small>}
            {field.name === 'chat_auto_speak_initial_delay_seconds' && <small>仅定时模式使用，设置启动后首次检查的延迟秒数。</small>}
            {field.name === 'chat_auto_speak_message_interval_seconds' && <small>仅消息模式使用，按群限制两次决策检查的最短间隔，单位秒；0 不限制，不影响定时模式。</small>}
            {field.name === 'chat_auto_speak_prompt' && <small>留空沿用人设与自动回复规则。message 使用触发时消息，interval 使用群里最新有效消息，无需填写生成引导句。</small>}
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
