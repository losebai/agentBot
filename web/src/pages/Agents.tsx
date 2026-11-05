import { Alert, Button } from 'antd';
import { CheckOutlined, DeleteOutlined, DownOutlined, PlusOutlined, ReloadOutlined, UserOutlined } from '@ant-design/icons';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { Dispatch, SetStateAction } from 'react';
import { AgentApi } from '../agent-api';
import { errorMessage } from '../api';
import type { Agent, AgentDraft } from '../agent-types';
import { AgentModelSelect } from '../components/AgentModelSelect';
import { AgentToolSelects } from '../components/AgentToolSelects';
import { dirtyFields } from '../components/DirectoryEditor';
import { RecordPagination } from '../components/RecordPagination';
import { CharacterPortrait, useCharacterPortraits } from '../components/CharacterPortrait';
import './Agents.css';

const empty:AgentDraft={name:'',code:'',description:'',enabled:false,model_id:0,vision_model_id:null,fallback_model_id:null,system_prompt:'',personality_prompt:'',tools:[],skill_ids:[],mcp_ids:[],options:{}};

/** 内置模板不写入数据库；用户明确保存后才创建正式角色并绑定模型。 */
const defaultCharacter: AgentDraft = { ...empty, name: '默认角色', code: 'default_assistant',
  description: '一个清晰、友善、可靠的日常助手。',
  system_prompt: '你是一位可靠的日常助手，帮助用户解答问题、整理信息和完成任务。优先给出清晰、可执行的建议；信息不足时说明不确定之处并请求必要补充，不编造事实，不越过用户授权边界。',
  personality_prompt: '友善、耐心、表达自然。先说重点，保持简洁，复杂问题分步骤说明，尊重用户的偏好和边界。' };

/** 人格为空时完整保留旧角色文本，启用人格后才按服务端规则分段。 */
function composePrompt(draft: AgentDraft): string {
  const personality = (draft.personality_prompt ?? '').trim();
  if (!personality) return draft.system_prompt;
  const role = draft.system_prompt.trim();
  return `${role ? `【角色设定】\n${role}\n\n` : ''}【人格设定】\n${personality}`;
}

/** 按 Unicode 字符计数，与服务端长度限制保持一致，避免把表情计作两个字符。 */
function promptLength(draft: AgentDraft): number {
  return Array.from(composePrompt(draft)).length;
}

/** 原始字段与组合文本都受上限约束，纯空白内容也不能绕过字段校验。 */
function promptError(draft: AgentDraft): string {
  for (const [label, text] of [['角色', draft.system_prompt], ['性格', draft.personality_prompt ?? '']]) {
    const length = Array.from(text).length;
    if (length > 32000) return `${label}不能超过 32000 字符，当前 ${length} 字符。`;
  }
  const length = promptLength(draft);
  return length > 32000 ? `组合提示词不能超过 32000 字符，当前 ${length} 字符（包含分段标题）。` : '';
}

/** 两项文本独立保留在当前 Agent 草稿中，预览和超限提示均不改写输入。 */
function AgentPromptFields({ draft, set }: {
  draft: AgentDraft; set: Dispatch<SetStateAction<AgentDraft>>;
}) {
  const prompt = composePrompt(draft);
  const length = promptLength(draft);
  const error = promptError(draft);
  return <>
    <div className="agent-prompt-columns">
      <div className="agent-prompt-card"><span className="agent-field-caption">角色设定</span><label>角色<textarea aria-label="角色" rows={10} placeholder="这个角色是谁？擅长什么？需要遵守哪些规则？" value={draft.system_prompt}
        onChange={e=>set(previous=>({...previous,system_prompt:e.target.value}))}/></label>
        <p className="field-help">定义职责、目标和行为规则。</p></div>
      <div className="agent-prompt-card"><span className="agent-field-caption">人格表达</span><label>性格<textarea aria-label="性格" rows={10} placeholder="例如：温和、好奇，回答简洁，偶尔带一点幽默。" value={draft.personality_prompt ?? ''}
        onChange={e=>set(previous=>({...previous,personality_prompt:e.target.value}))}/></label>
        <p className="field-help">定义性格、语气和表达习惯，留空沿用角色原文。</p></div>
    </div>
    {error && <Alert type="error" showIcon message={error}/>}
    <details className="agent-prompt-preview">
      <summary>组合提示词预览 · {length} / 32000 字符</summary>
      <pre aria-label="组合提示词预览">{prompt}</pre>
    </details>
  </>;
}

interface AgentEdit { original: Agent | null; value: AgentDraft }

/** 仅复制可写字段；隐藏的历史 Skill 关联保留，缺失的性格字段正规化为空。 */
function editableAgent(agent?: Agent): AgentDraft {
  return Object.fromEntries(Object.entries(empty).map(([key, value]) => [
    key, agent?.[key as keyof AgentDraft] ?? value,
  ])) as unknown as AgentDraft;
}

/** 页内切换多个 Agent，草稿及写入版本按身份隔离，不使用编辑弹窗。 */
export function Agents({ token, active = true }: { token: string; active?: boolean }) {
  const api = useMemo(() => new AgentApi(token), [token]);
  const [items, setItems] = useState<Agent[]>([]);
  const [total, setTotal] = useState(0);
  const [pageNumber, setPageNumber] = useState(1), [pageSize, setPageSize] = useState(10);
  const [selected, setSelected] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, AgentEdit>>({});
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [promptExpanded, setPromptExpanded] = useState(true);
  const [abilitiesExpanded, setAbilitiesExpanded] = useState(true);
  const [refresh, setRefresh] = useState(0);
  const pending = useRef<AbortController>();
  const selectedRef = useRef(selected);
  selectedRef.current = selected;
  const alive = useRef(true);
  const wasActive = useRef(active);
  const errorRef = useRef<HTMLDivElement>(null);
  const portraits = useCharacterPortraits(api, items);

  /** 刷新只替换目录；已经编辑的草稿仍绑定原快照，不能绕过乐观锁覆盖外部修改。 */
  const load = useCallback(async () => {
    pending.current?.abort();
    const controller = new AbortController();
    pending.current = controller;
    setLoading(true); setError('');
    try {
      const page = await api.agentPage(pageNumber, pageSize, controller.signal);
      if (controller.signal.aborted) return;
      const current = selectedRef.current;
      const pageTotal = Number.isInteger(page.total)
        ? page.total : page.items.length + (page.next_cursor ? pageSize : 0);
      setItems(page.items); setTotal(pageTotal);
      const last = Math.max(1, Math.ceil(pageTotal / pageSize));
      if (pageNumber > last) setPageNumber(last);
      setSelected(previous => previous === 'new' || previous === 'default' || (previous && page.items.some(agent => String(agent.id) === previous))
        ? previous : (page.items[0] ? String(page.items[0].id) : null));
      if (current !== 'new' && current && !page.items.some(agent => String(agent.id) === current)) setConfirmDelete(false);
      setRefresh(value => value + 1);
    } catch (failure) {
      if (!controller.signal.aborted) setError(errorMessage(failure));
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }, [api, pageNumber, pageSize]);

  useEffect(() => {
    alive.current = true;
    void load();
    // 页面离开或登录凭据更新时取消尚未结束的目录查询。
    return () => { alive.current = false; pending.current?.abort(); };
  }, [load]);
  useEffect(() => {
    // 页面保持挂载以保存草稿；重新进入时同步运行角色排序，写入期间延后读取。
    if (!active) wasActive.current = false;
    else if (!busy && !wasActive.current) {
      wasActive.current = true;
      void load();
    }
  }, [active, busy, load]);
  useEffect(() => {
    // 长页面保存失败后将焦点移到错误，使浏览器滚入可见区域。
    if (error) errorRef.current?.focus();
  }, [error]);

  const item = items.find(agent => String(agent.id) === selected);
  const isCreating = selected === 'new' || selected === 'default';
  const edit = selected ? drafts[selected] : undefined;
  const original = edit?.original ?? item;
  const draft = edit?.value ?? (item ? editableAgent(item) : null);

  /** 只更新当前身份的草稿，提交期间忽略控件回调，防止响应覆盖新输入。 */
  const setDraft: Dispatch<SetStateAction<AgentDraft>> = value => {
    if (!selected || !draft || busy) return;
    const key = selected;
    setDrafts(previous => ({ ...previous, [key]: {
      original: previous[key]?.original ?? item ?? null,
      value: typeof value === 'function' ? value(previous[key]?.value ?? draft) : value,
    } }));
    setNotice(''); setConfirmDelete(false);
  };

  /** 切换仅改变展示身份，不丢弃其他 Agent 或新建 Agent 的未保存内容。 */
  function selectAgent(key: string) {
    if (busy || loading) return;
    setSelected(key); setError(''); setNotice(''); setConfirmDelete(false);
  }

  /** 新建也在当前页面编辑；重复点击新增回到已有新建草稿。 */
  function create() {
    setDrafts(previous => previous.new ? previous : {
      ...previous, new: { original: null, value: editableAgent() },
    });
    selectAgent('new');
  }

  /** 默认模板单独保留草稿，反复打开不会覆盖尚未保存的输入。 */
  function useDefaultCharacter() {
    setDrafts(previous => previous.default ? previous : {
      ...previous, default: { original: null, value: { ...defaultCharacter } },
    });
    selectAgent('default');
  }

  /** 保存只提交已编辑字段；身份或启用状态改变后同步服务端排序，保留当前编辑选择。 */
  async function save() {
    if (!draft || !selected || busy || promptError(draft)) return;
    const key = selected;
    // 写入优先于目录读取；即使旧响应忽略取消并晚到，也不能回填过时快照。
    pending.current?.abort(); setLoading(false);
    setBusy(true); setError(''); setNotice('');
    try {
      const portrait = await portraits.upload(key);
      const changes = { ...(original ? dirtyFields(editableAgent(original), draft) : draft),
        ...(portrait ? { portrait } : {}) };
      if (original && !Object.keys(changes).length) {
        setNotice('没有需要保存的修改。');
        return;
      }
      const saved = original
        ? await api.updateAgent(original.id, original.revision, changes)
        : await api.createAgent(changes as AgentDraft);
      if (!alive.current) return;
      setItems(previous => previous.some(agent => agent.id === saved.id)
        ? previous.map(agent => agent.id === saved.id ? { ...saved, is_current: agent.is_current && saved.enabled } : agent) : [...previous, saved]);
      setDrafts(previous => { const next = { ...previous }; delete next[key]; return next; });
      portraits.move(key, String(saved.id));
      setSelected(String(saved.id)); setNotice('Agent 已保存。'); setConfirmDelete(false);
      if (!original) {
        setTotal(value => value + 1);
        setPageNumber(Math.max(1, Math.ceil((total + 1) / pageSize)));
      }
      if (!original || original.enabled !== saved.enabled || original.code !== saved.code) {
        // 新建、启停或改代码可能影响正在使用的角色，不能让保存前的目录顺序继续留在页面。
        selectedRef.current = String(saved.id);
        if (original) await load();
      }
    } catch (failure) {
      if (alive.current) setError(errorMessage(failure));
    } finally { if (alive.current) setBusy(false); }
  }

  /** 撤销仅丢弃当前身份的草稿；取消新建后回到已有 Agent。 */
  function discard() {
    if (!selected) return;
    portraits.reset(selected);
    setDrafts(previous => { const next = { ...previous }; delete next[selected]; return next; });
    if (isCreating) setSelected(items[0] ? String(items[0].id) : null);
    setError(''); setNotice(''); setConfirmDelete(false); setRefresh(value => value + 1);
  }

  /** 删除前在页面内确认，只有已保存为停用状态的 Agent 才可删除。 */
  async function remove() {
    if (!original || original.enabled || busy) return;
    const id = original.id;
    // 删除提交前停止目录刷新，防止旧列表让已删除的身份重新出现。
    pending.current?.abort(); setLoading(false);
    setBusy(true); setError('');
    try {
      await api.deleteAgent(id, original.revision);
      if (!alive.current) return;
      const remaining = items.filter(agent => agent.id !== id);
      portraits.remove(String(id));
      setItems(remaining);
      setTotal(value => Math.max(0, value - 1));
      setDrafts(previous => { const next = { ...previous }; delete next[String(id)]; return next; });
      setSelected(remaining[0] ? String(remaining[0].id) : drafts.new ? 'new' : null);
      setConfirmDelete(false); setNotice('Agent 已删除。');
      if (!remaining.length && pageNumber > 1) setPageNumber(value => value - 1);
    } catch (failure) {
      if (alive.current) setError(errorMessage(failure));
    } finally { if (alive.current) setBusy(false); }
  }

  return <div className="page-content agent-page">
    <div className="page-heading"><div><p className="eyebrow">CHARACTER STUDIO</p>
      <h1>角色和人格</h1><p>赋予每个角色独特的形象、职责与表达方式。</p></div>
      <div className="heading-actions"><Button aria-label="新增 Agent" type="primary" icon={<PlusOutlined />} disabled={busy || loading} onClick={create}>新增 Agent</Button>
        <Button aria-label="删除 Agent" danger icon={<DeleteOutlined aria-hidden="true" />}
          disabled={busy || !original || original.enabled || Boolean(draft?.enabled)}
          onClick={() => setConfirmDelete(true)}>删除 Agent</Button>
        <Button aria-label="刷新" icon={<ReloadOutlined />} disabled={busy} loading={loading} onClick={() => void load()}>刷新</Button></div>
    </div>
        {confirmDelete && original && <div className="agent-delete-confirm" role="group" aria-label="删除确认">
          <p>确定删除“{original.name}”？此操作也会丢弃当前 Agent 的未保存修改。</p>
          <Button danger disabled={busy} onClick={() => void remove()}>确认删除</Button>
          <Button disabled={busy} onClick={() => setConfirmDelete(false)}>取消删除</Button>
        </div>}
    <div className="agent-gallery-heading"><h2>我的角色 <span>{total}</span></h2><span>选择一张角色卡片开始编辑</span></div>
    <div className="agent-switcher agent-card-grid" role="group" aria-label="切换 Agent">
      {/* 默认模板紧随运行角色；目录本身由后端在分页前完成运行角色置顶与时间排序。 */}
      {(() => {
        const current = items.find(agent => agent.is_current);
        const cards: (Agent | null)[] = pageNumber === 1
          ? (current ? [current, null, ...items.filter(agent => agent.id !== current.id)] : [null, ...items])
          : items;
        return cards.map((agent, index) => agent ? <button key={agent.id} type="button"
          className={`agent-character-card agent-tone-${index % 4}`}
          aria-label={agent.name} aria-pressed={selected === String(agent.id)} disabled={busy || loading}
          onClick={() => selectAgent(String(agent.id))}>
          <span className="agent-card-cover">
            {portraits.images[String(agent.id)] ? <img src={portraits.images[String(agent.id)]} alt="" /> : <UserOutlined aria-hidden="true" />}
            <span className={`agent-card-status${agent.is_current ? ' is-current' : agent.enabled ? ' is-enabled' : ''}`}>
              {agent.is_current ? '正在使用' : agent.enabled ? '已启用' : '未启用'}</span>
            {selected === String(agent.id) && <span className="agent-card-check" aria-hidden="true"><CheckOutlined /></span>}
          </span>
          <span className="agent-card-body"><strong>{agent.name}</strong><span>{agent.description || '还没有添加角色介绍'}</span>
            <small>{drafts[String(agent.id)] ? '有未保存的修改' : `@${agent.code}`}</small></span>
        </button> : <button key="default" type="button" className="agent-character-card agent-default-card"
          aria-label="默认角色模板" aria-pressed={selected === 'default'} disabled={busy || loading} onClick={useDefaultCharacter}>
          <span className="agent-card-cover"><UserOutlined aria-hidden="true" /><span className="agent-card-status">内置模板</span>
            {selected === 'default' && <span className="agent-card-check" aria-hidden="true"><CheckOutlined /></span>}</span>
          <span className="agent-card-body"><strong>默认角色</strong><span>{defaultCharacter.description}</span><small>选择模型并保存为新角色</small></span>
        </button>);
      })()}
      {drafts.new && <button type="button" className="agent-character-card agent-new-card"
        aria-pressed={selected === 'new'} aria-label={drafts.new.value.name || '新 Agent'} disabled={busy || loading}
        onClick={() => selectAgent('new')}><span className="agent-card-cover" aria-hidden="true"><PlusOutlined /></span>
        <span className="agent-card-body"><strong>{drafts.new.value.name || '新 Agent'}</strong><span>继续完善你的新角色</span><small>未保存的草稿</small></span></button>}
    </div>
    <RecordPagination current={pageNumber} pageSize={pageSize} total={total} loading={loading}
      hasNext={pageNumber * pageSize < total} onChange={(number,size)=>{setPageNumber(number);setPageSize(size);}}/>
    {error && <div ref={errorRef} role="group" aria-label="操作错误" tabIndex={-1} className="notice">
      <Alert type="error" showIcon message={error}/></div>}
    {notice && <Alert type="success" showIcon className="notice" message={notice}/>}
    {!draft && <div className="panel empty-list">{loading ? '正在加载…' : '暂无 Agent，点击“新增 Agent”开始配置。'}</div>}
    {draft && <section className="agent-character-overview" aria-label="当前角色预览">
      <CharacterPortrait image={selected ? portraits.images[selected] : undefined} name={draft.name || '新角色'} disabled={busy}
        onChange={file => { if (selected) portraits.choose(selected, file); }} error={selected ? portraits.errors[selected] ?? '' : ''} />
      <div className="agent-character-intro"><span className="agent-section-kicker">{isCreating ? '创建中的角色' : '当前编辑角色'}</span>
        <div className="agent-intro-title"><h2>{draft.name || '为角色取个名字'}</h2><span className={`agent-identity-status${draft.enabled ? ' is-enabled' : ''}`}>{draft.enabled ? '已启用' : '未启用'}</span></div>
        <p>{draft.description || '从一个名字、一段故事开始，让你的 Agent 拥有鲜明的人格。'}</p>
        <div className="agent-identity-chips"><span>@{draft.code || 'agent_code'}</span>
          <span>{draft.personality_prompt?.trim() ? '已设定性格' : '待设定性格'}</span>
          <span>{edit ? '有未保存的修改' : '配置已保存'}</span></div>
        <dl className="agent-character-facts"><div><dt>角色设定</dt><dd>{Array.from(draft.system_prompt).length}<small> 字</small></dd></div>
          <div><dt>性格设定</dt><dd>{Array.from(draft.personality_prompt ?? '').length}<small> 字</small></dd></div>
          <div><dt>工具与服务</dt><dd>{draft.tools.length + draft.mcp_ids.length}<small> 项</small></dd></div></dl>
        <p className="agent-preview-note">下方修改会实时反映在预览中，保存后角色配置才会生效。</p>
      </div>
    </section>}
    {draft && <form className="catalog-form panel agent-editor" aria-label="Agent 配置"
      onSubmit={event => { event.preventDefault(); void save(); }}>
      <fieldset className="agent-editor-fields" disabled={busy}>
        {/* 保存留在表单内维持原生校验，标题操作区随窄屏换行。 */}
        <div className="agent-basics-header">
          <div className="agent-editor-section-title"><span>01</span><div><h2>基本信息</h2><p>让角色拥有清晰的身份。</p></div></div>
          <div className="heading-actions agent-basics-actions">
            <Button disabled={busy} onClick={discard}>{isCreating ? '取消新建' : '撤销修改'}</Button>
            <Button type="primary" htmlType="submit" loading={busy} disabled={busy || Boolean(promptError(draft)) || Boolean(selected && portraits.preparing[selected])}>保存 Agent</Button>
          </div>
        </div>
        <div className="agent-basics">
          <label>名称<input required maxLength={200} value={draft.name}
            onChange={event => setDraft(previous => ({ ...previous, name: event.target.value }))}/></label>
          <label>Agent 代码<input required pattern="[a-z][a-z0-9_-]{0,63}" value={draft.code}
            onChange={event => setDraft(previous => ({ ...previous, code: event.target.value }))}/></label>
          <label className="checkbox-label"><input type="checkbox" checked={draft.enabled}
            onChange={event => setDraft(previous => ({ ...previous, enabled: event.target.checked }))}/>启用 Agent</label>
        </div>
        <label>说明<input maxLength={2000} value={draft.description}
          onChange={event => setDraft(previous => ({ ...previous, description: event.target.value }))}/></label>
        {/* 折叠只隐藏内容，保留草稿与子组件状态；基本信息中的保存按钮保持可见。 */}
        <div className="agent-editor-section-title agent-collapsible-title"><span>02</span><div>
          <h2><button type="button" aria-expanded={promptExpanded} aria-controls="agent-personality-fields"
            onClick={() => setPromptExpanded(value => !value)}>角色与人格<DownOutlined aria-hidden="true" /></button></h2>
          <p>职责决定做什么，性格决定如何表达。</p></div></div>
        <div id="agent-personality-fields" className="agent-collapsible-body" hidden={!promptExpanded}>
          <AgentPromptFields draft={draft} set={setDraft}/>
        </div>
        <div className="agent-editor-section-title agent-collapsible-title"><span>03</span><div>
          <h2><button type="button" aria-expanded={abilitiesExpanded} aria-controls="agent-ability-fields"
            onClick={() => setAbilitiesExpanded(value => !value)}>模型与能力<DownOutlined aria-hidden="true" /></button></h2>
          <p>配置角色使用的模型，以及允许调用的工具。</p></div></div>
        <div id="agent-ability-fields" className="agent-collapsible-body" hidden={!abilitiesExpanded}>
        <AgentModelSelect key={`${selected}-${refresh}`} token={token} value={draft.model_id}
          visionValue={draft.vision_model_id ?? null} fallbackValue={draft.fallback_model_id ?? null}
          onChange={model_id => setDraft(previous => ({ ...previous, model_id }))}
          onVisionChange={vision_model_id => setDraft(previous => ({ ...previous, vision_model_id }))}
          onFallbackChange={fallback_model_id => setDraft(previous => ({ ...previous, fallback_model_id }))}/>
        <Alert type="info" message="工具调用次数在“机器人配置 → LLM设置”中统一调整。"/>
        <AgentToolSelects key={`tools-${selected}-${refresh}`} token={token} tools={draft.tools} mcpIds={draft.mcp_ids}
          onToolsChange={tools => setDraft(previous => ({ ...previous, tools }))}
          onMcpsChange={mcp_ids => setDraft(previous => ({ ...previous, mcp_ids }))}/>
        </div>
      </fieldset>
    </form>}
  </div>;
}
