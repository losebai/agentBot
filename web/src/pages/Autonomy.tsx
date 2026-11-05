import { AgentUsageGuide } from '../components/AgentUsageGuide';
import { Alert, Button, Checkbox, Descriptions, Empty, Form, Input, InputNumber, Modal, Select, Space, Switch, Table, Tabs, Tag, Tooltip } from 'antd';
import { CheckOutlined, CloseOutlined, EditOutlined, PauseOutlined, PlusOutlined, ReloadOutlined, SafetyCertificateOutlined } from '@ant-design/icons';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AutonomyApi, participant } from '../autonomy-api';
import type { Action, AuditEntry, AutonomousTask, AutonomyPolicy, MemoryProposal, PersistentIdentity, Profile } from '../autonomy-api';
import { ApiClient, errorMessage } from '../api';
import { ModelSelect } from '../components/ModelSelect';
import { useModelDirectory } from '../components/useModelDirectory';
import { AutonomyEvolution } from '../components/AutonomyEvolution';
import { AutonomyConversation } from '../components/AutonomyConversation';
import './Autonomy.css';

const actions: { label: string; value: Action }[] = [
  { label: '计划', value: 'plan' }, { label: '长期检索', value: 'recall' }, { label: '文字整理', value: 'draft' },
  { label: '情节归档', value: 'consolidate' }, { label: '每日复盘', value: 'review' }, { label: '汇报', value: 'report' },
];
const names: Record<string, string> = { pending: '待执行', running: '执行中', retry: '等待重试',
  blocked: '策略等待', approval: '待审批', succeeded: '已完成', failed: '失败', cancelled: '已取消',
  applied: '已写入', applying: '写入中', rejected: '已拒绝', review: '待核对', sleeping: '休眠',
  focused: '专注', neutral: '平静', uncertain: '不确定', concerned: '关注错误' };

/** 持续身份工作台：设定、当前状态、任务与记忆分栏，所有副作用经过宿主接口。 */
export function Autonomy({ token }: { token: string }) {
  const api = useMemo(() => new AutonomyApi(token), [token]);
  const base = useMemo(() => new ApiClient(token), [token]);
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [selected, setSelected] = useState('');
  const [tasks, setTasks] = useState<AutonomousTask[]>([]);
  const [proposals, setProposals] = useState<MemoryProposal[]>([]);
  const [audit, setAudit] = useState<AuditEntry[]>([]);
  const [before, setBefore] = useState<string | null>(null);
  const [memories, setMemories] = useState<Record<string, unknown[]>>({});
  const [query, setQuery] = useState('记忆');
  const [tab, setTab] = useState('conversation');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [modal, setModal] = useState<'create' | 'identity' | 'policy' | 'state' | 'task' | 'event' | null>(null);
  const [details, setDetails] = useState<unknown>(null);
  const [hostPaused, setHostPaused] = useState(false);
  const [capture, setCapture] = useState(false);
  const [form] = Form.useForm();
  const directory = useModelDirectory(token, modal === 'policy');
  const pending = useRef<AbortController>();
  const profile = profiles.find(item => item.id === selected);
  const source = Form.useWatch('source', form);

  /** 切换身份或离开页面时取消旧读取，避免后返回的审计覆盖当前身份。 */
  const load = useCallback(async () => {
    pending.current?.abort(); const controller = new AbortController(); pending.current = controller;
    setError('');
    try {
      const settings = await base.settings(controller.signal);
      const items: Profile[] = [];
      let after = '';
      let page: Profile[];
      do {
        page = await api.profiles(after, controller.signal);
        items.push(...page);
        after = page.at(-1)?.id || '';
      } while (page.length === 50 && !controller.signal.aborted);
      if (controller.signal.aborted) return;
      setProfiles(items);
      setHostPaused(settings.fields.find(field => field.name === 'chat_active_actions_paused')?.value === true);
      setCapture(settings.fields.find(field => field.name === 'chat_autonomy_capture_enabled')?.value === true);
      if (!selected && items.length) { setSelected((items.find(item => item.owner.channel === 'local') || items[0]).id); return; }
      if (selected && items.some(item => item.id === selected)) {
        const [newTasks, newProposals, history] = await Promise.all([
          api.tasks(selected, '', controller.signal), api.proposals(selected, '', controller.signal), api.audit(selected, '', controller.signal),
        ]);
        if (controller.signal.aborted) return;
        setTasks(newTasks); setProposals(newProposals); setAudit(history.items); setBefore(history.next_before);
      }
    } catch (failure) { if (!controller.signal.aborted) setError(errorMessage(failure)); }
  }, [api, base, selected]);
  useEffect(() => { void load(); return () => pending.current?.abort(); }, [load]);

  /** 写操作成功才刷新列表，失败保留表单草稿与当前选择。 */
  async function operate(action: () => Promise<unknown>, close = false, refresh = true) {
    if (busy) return;
    setBusy(true); setError('');
    try { await action(); if (close) setModal(null); if (refresh) await load(); }
    catch (failure) { setError(errorMessage(failure)); }
    finally { setBusy(false); }
  }
  /** 宿主开关只提交当前编辑字段，不回写其他旧配置。 */
  async function changeHost(field: string, value: boolean) {
    await operate(async () => { const snapshot = await base.settings(); await base.updateSettings(snapshot.database_revision, { [field]: value }); });
  }
  /** 模态框仅编辑所选对象，创建后的所有者绑定不可在网页改写。 */
  function open(mode: typeof modal) {
    if (busy) return;
    form.resetFields();
    if (mode === 'identity') form.setFieldsValue(profile?.identity);
    if (mode === 'policy') form.setFieldsValue(profile?.policy);
    if (mode === 'state') form.setFieldsValue(profile?.state);
    if (mode === 'create') form.setFieldsValue({ participant_id: 'owner' });
    if (mode === 'event') form.setFieldsValue({ source: 'self_report', task_requested: false });
    setModal(mode);
  }
  /** 版本绑定来自已加载对象；后端冲突不会自动重试覆盖。 */
  async function submit() {
    let values: Record<string, unknown>;
    try { values = await form.validateFields(); } catch { return; }
    await operate(async () => {
      if (modal === 'create') {
        const created = await api.create({ owner: { channel: 'local', agent_key: crypto.randomUUID(),
          participant_id: String(values.participant_id), session_id: crypto.randomUUID() },
        identity: { name: String(values.name), role: '', style: '', relationships: '', boundaries: '' } });
        setSelected(created.id);
      } else if (profile) {
        if (modal === 'identity') await api.update(profile, { identity: values as unknown as PersistentIdentity });
        if (modal === 'policy') await api.update(profile, { policy: { ...profile.policy, ...values } as AutonomyPolicy });
        if (modal === 'state') await api.update(profile, { state: { ...profile.state, ...values } });
        if (modal === 'task') await api.createTask(profile.id, String(values.goal), values.run_at ? new Date(String(values.run_at)).toISOString() : null);
        if (modal === 'event') await api.ingest(profile.id, String(values.text), values.task_requested === true, String(values.source));
      }
    }, true, modal !== 'create');
  }
  /** 记忆搜索单独读取，不因任务刷新抹掉搜索内容。 */
  async function search() {
    if (profile && query.trim()) await operate(async () => { setMemories(await api.memories(profile.id, query)); });
  }

  return <div className="page-content autonomy-page">
    <div className="page-heading"><h1>持续智能体</h1><Space wrap>
      <Tooltip title="刷新"><Button icon={<ReloadOutlined />} aria-label="刷新持续智能体" disabled={busy} onClick={() => void load()} /></Tooltip>
      <Button icon={<PlusOutlined />} onClick={() => open('create')}>新建身份</Button>
      <Button danger icon={<PauseOutlined />} disabled={busy} onClick={() => void operate(() => api.pause())}>暂停全部身份</Button>
    </Space></div>
    <AgentUsageGuide feature="autonomy" />
    {(error || directory.error) && <Alert type="error" showIcon message={error || directory.error} />}
    <div className="autonomy-host"><label>主动行动总暂停 <Switch aria-label="主动行动总暂停" checked={hostPaused} disabled={busy} onChange={value => void changeHost('chat_active_actions_paused', value)} /></label>
      <label>会话事件归档 <Switch aria-label="会话事件归档" checked={capture} disabled={busy} onChange={value => void changeHost('chat_autonomy_capture_enabled', value)} /></label></div>
    <div className="autonomy-toolbar"><Select aria-label="持续身份" disabled={busy} value={selected || undefined} onChange={id => { pending.current?.abort(); setTasks([]); setProposals([]); setAudit([]); setBefore(null); setSelected(id); setMemories({}); setDetails(null); setModal(null); }}
      placeholder="选择持续身份" options={profiles.map(item => ({ value: item.id, label: `${item.identity.name} · ${participant(item.owner)}` }))} />
      {profile && <><Tag color={profile.policy.paused ? 'default' : 'green'}>{profile.policy.paused ? '已暂停' : '已启用'}</Tag>
        <Button onClick={() => void operate(() => api.update(profile, { policy: { ...profile.policy, paused: !profile.policy.paused } }))} disabled={busy}>{profile.policy.paused ? '启用身份' : '暂停身份'}</Button>
        <Button icon={<EditOutlined />} onClick={() => open('identity')}>身份设定</Button>
        <Button onClick={() => open('policy')}>宿主策略</Button></>}
    </div>
    {!profile ? <Empty description="暂无持续身份" /> : <Tabs activeKey={tab} onChange={setTab} items={[
      { key: 'conversation', label: '循环对话', children: <AutonomyConversation key={profile.id} api={api} profile={profile} active={tab === 'conversation'} /> },
      { key: 'evolution', label: '自我演进', children: <AutonomyEvolution key={profile.id} api={api} id={profile.id} /> },
      { key: 'state', label: '当前状态', children: <>
        <Descriptions bordered size="small" column={{ xs: 1, md: 2 }} items={[
          { key: 'id', label: '稳定身份', children: profile.id }, { key: 'rev', label: '设定版本', children: profile.revision },
          { key: 'role', label: '角色', children: profile.identity.role || '未设置' }, { key: 'style', label: '表达风格', children: profile.identity.style || '未设置' },
          { key: 'goal', label: '当前目标', children: profile.state.current_goal || '无' }, { key: 'activity', label: '正在做', children: names[profile.state.activity] || actions.find(item => item.value === profile.state.activity)?.label || profile.state.activity },
          { key: 'emotion', label: '表达状态', children: names[profile.state.emotion] }, { key: 'task', label: '在途任务', children: profile.state.current_task || '无' },
          { key: 'calls', label: '今日模型调用', children: profile.usage.model_calls || 0 }, { key: 'cost', label: '今日预留费用', children: (profile.usage.reserved_cost || 0).toFixed(6) },
          { key: 'cap', label: '能力边界', children: profile.policy.allowed_actions.map(action => actions.find(item => item.value === action)?.label).join('、') },
          { key: 'errors', label: '最近错误', children: profile.state.recent_errors.join('、') || '无' },
        ]} />
        <Space className="autonomy-actions" wrap><Button onClick={() => open('state')}>更新状态</Button><Button onClick={() => open('event')}>记录情节</Button>
          <Button icon={<SafetyCertificateOutlined />} loading={busy} onClick={() => void operate(async () => { setDetails(await api.evaluate(profile.id)); })}>运行离线评测</Button></Space>
      </> },
      { key: 'tasks', label: `任务 (${tasks.length})`, children: <><Button icon={<PlusOutlined />} onClick={() => open('task')}>新建目标</Button>
        <Table rowKey="id" dataSource={tasks} scroll={{ x: 780 }} pagination={false} columns={[
          { title: '目标', dataIndex: 'goal', width: 260, render: (value: string, row: AutonomousTask) => <Button type="link" onClick={() => setDetails(row)}>{value}</Button> },
          { title: '状态', dataIndex: 'status', render: (value: string) => <Tag>{names[value] || value}</Tag> },
          { title: '步骤', render: (_, row: AutonomousTask) => `${row.cursor}/${row.steps.length}` },
          { title: '错误', dataIndex: 'error_code' },
          { title: '操作', render: (_, row: AutonomousTask) => <Space>{row.status === 'approval' && <Tooltip title="批准当前计划"><Button icon={<CheckOutlined />} aria-label="批准当前计划" disabled={busy} onClick={() => void operate(() => api.decideTask(profile.id, row, true))} /></Tooltip>}
            {!['succeeded', 'failed', 'cancelled'].includes(row.status) && <Tooltip title="取消任务"><Button danger icon={<CloseOutlined />} aria-label="取消任务" disabled={busy} onClick={() => void operate(() => api.decideTask(profile.id, row, false))} /></Tooltip>}</Space> },
        ]} />{tasks.length >= 50 && <Button disabled={busy} onClick={() => void operate(async () => { const more = await api.tasks(profile.id, tasks[tasks.length - 1].id); setTasks(old => [...old, ...more]); }, false, false)}>更多任务</Button>}</> },
      { key: 'memories', label: '分层记忆', children: <><Space.Compact className="autonomy-search"><Input aria-label="记忆查询" value={query} maxLength={200} onChange={event => setQuery(event.target.value)} onPressEnter={() => void search()} /><Button onClick={() => void search()} disabled={busy}>检索</Button></Space.Compact>
        {(['episodic', 'facts', 'relationships', 'task_memories'] as const).map((key, index) => <section key={key}><h3>{['情节', '事实与偏好', '关系', '任务记忆'][index]}</h3>
          <Table rowKey={(_, rowIndex) => String(rowIndex)} dataSource={(memories[key] || []).map(item => item as Record<string, unknown>)} pagination={{ pageSize: 10 }} scroll={{ x: 560 }} columns={[
            { title: '记录', dataIndex: 'id', width: 130, ellipsis: true }, { title: '内容', render: (_, row) => String(row.content || row.summary || row.text || '') },
            { title: '详情', width: 70, render: (_, row) => <Button type="link" onClick={() => setDetails(row)}>查看</Button> },
          ]} /></section>)}</> },
      { key: 'proposals', label: `记忆提案 (${proposals.length})`, children: <><Table rowKey="id" dataSource={proposals} scroll={{ x: 730 }} pagination={false} columns={[
        { title: '事实', render: (_, row: MemoryProposal) => <span className="autonomy-text">{row.document.fact.content}</span> },
        { title: '原文证据', render: (_, row: MemoryProposal) => <span className="autonomy-text">{row.document.fact.excerpt}</span> },
        { title: '状态', dataIndex: 'status', render: (value: string) => names[value] || value },
        { title: '操作', render: (_, row: MemoryProposal) => <Space><Button type="link" onClick={() => setDetails(row)}>核对</Button>
          {['pending', 'review'].includes(row.status) && <><Tooltip title="批准提案"><Button icon={<CheckOutlined />} aria-label="批准提案" disabled={busy} onClick={() => void operate(() => api.decideProposal(profile.id, row, true))} /></Tooltip>
            <Tooltip title="拒绝提案"><Button danger icon={<CloseOutlined />} aria-label="拒绝提案" disabled={busy} onClick={() => void operate(() => api.decideProposal(profile.id, row, false))} /></Tooltip></>}</Space> },
      ]} />{proposals.length >= 50 && <Button disabled={busy} onClick={() => void operate(async () => { const more = await api.proposals(profile.id, proposals[proposals.length - 1].id); setProposals(old => [...old, ...more]); }, false, false)}>更多提案</Button>}</> },
      { key: 'audit', label: '审计与评估', children: <><Table rowKey="id" dataSource={audit} scroll={{ x: 650 }} pagination={false} columns={[
        { title: '时间', dataIndex: 'created_at', width: 210 }, { title: '事件', dataIndex: 'kind' },
        { title: '关联记录', dataIndex: 'subject_id', ellipsis: true }, { title: '详情', render: (_, row: AuditEntry) => <Button type="link" onClick={() => setDetails(row.document_json)}>查看</Button> },
      ]} />{before && <Button disabled={busy} onClick={() => void operate(async () => { const page = await api.audit(profile.id, before); setAudit(old => [...old, ...page.items]); setBefore(page.next_before); }, false, false)}>更早记录</Button>}</> },
    ]} />}
    <Modal title={{ create: '新建持续身份', identity: '身份设定', policy: '宿主策略', state: '当前状态', task: '新建目标', event: '记录情节' }[modal || 'create']}
      open={modal !== null} onCancel={() => setModal(null)} onOk={() => void submit()} confirmLoading={busy} destroyOnHidden>
      <Form form={form} layout="vertical">
        {modal === 'create' && <><Form.Item name="name" label="身份名称" rules={[{ required: true }]}><Input maxLength={100} /></Form.Item>
          <Form.Item name="participant_id" label="参与者标识" rules={[{ required: true }]}><Input maxLength={100} /></Form.Item></>}
        {modal === 'identity' && <>{(['name', 'role', 'style', 'relationships', 'boundaries'] as const).map((key, index) => <Form.Item key={key} name={key} label={['名称', '角色', '表达风格', '关系设定', '行为边界'][index]} rules={key === 'name' ? [{ required: true }] : []}>{key === 'name' ? <Input maxLength={100} /> : <Input.TextArea rows={3} maxLength={key === 'role' ? 6000 : 2000} />}</Form.Item>)}</>}
        {modal === 'policy' && <><Form.Item name="allowed_actions" label="动作白名单"><Checkbox.Group options={actions} /></Form.Item>
          <Form.Item name="model" label="模型"><ModelSelect models={directory.page.items} value={form.getFieldValue('model') || ''} nullable onChange={value => form.setFieldValue('model', value || '')} onOpen={() => void directory.load()} /></Form.Item>
          {(['discover_tasks', 'auto_consolidate', 'auto_correct', ...(profile?.owner.channel === 'local' ? [] : ['notify_qq'])] as ('discover_tasks' | 'auto_consolidate' | 'auto_correct' | 'notify_qq')[]).map((key, index) => <Form.Item key={key} name={key} label={['发现任务', '自动提取记忆', '自动更正本人事实', 'QQ 主动汇报'][index]} valuePropName="checked"><Switch /></Form.Item>)}
          {(['max_model_calls_per_day', 'daily_cost_limit', 'input_cost_per_million', 'output_cost_per_million', 'max_attempts', 'min_report_interval_seconds', 'max_reports_per_day', 'quiet_start_hour', 'quiet_end_hour', 'episode_retention_days'] as const).map((key, index) => <Form.Item key={key} name={key} label={['每日模型调用上限', '每日费用上限', '每百万输入 token 单价', '每百万输出 token 单价', '每步尝试上限', '汇报间隔（秒）', '每日汇报上限', '静默开始（北京时间）', '静默结束（北京时间）', '情节保留天数'][index]}><InputNumber min={0} style={{ width: '100%' }} /></Form.Item>)}</>}
        {modal === 'state' && <><Form.Item name="current_goal" label="当前目标"><Input.TextArea maxLength={2000} /></Form.Item><Form.Item name="emotion" label="表达状态"><Select options={['neutral', 'focused', 'uncertain', 'concerned'].map(value => ({ value, label: names[value] }))} /></Form.Item></>}
        {modal === 'task' && <><Form.Item name="goal" label="目标" rules={[{ required: true }]}><Input.TextArea rows={5} maxLength={4000} /></Form.Item><Form.Item name="run_at" label="执行时间"><Input type="datetime-local" /></Form.Item></>}
        {modal === 'event' && <><Form.Item name="text" label="情节原文" rules={[{ required: true }]}><Input.TextArea rows={6} maxLength={8000} /></Form.Item>
          <Form.Item name="source" label="来源"><Select options={[{ value: 'self_report', label: '本人发言' }, { value: 'third_party', label: '第三方资料' }]} onChange={value => { if (value === 'third_party') form.setFieldValue('task_requested', false); }} /></Form.Item>
          <Form.Item name="task_requested" label="请求执行任务" valuePropName="checked"><Switch disabled={source === 'third_party'} /></Form.Item></>}
      </Form>
    </Modal>
    <Modal title="记录详情" open={details !== null} footer={null} onCancel={() => setDetails(null)} width={760}><pre className="autonomy-detail">{JSON.stringify(details, null, 2)}</pre></Modal>
  </div>;
}
