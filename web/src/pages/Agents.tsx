import { Alert, Button } from 'antd';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { Dispatch, SetStateAction } from 'react';
import { AgentApi } from '../agent-api';
import { errorMessage } from '../api';
import type { Agent, AgentDraft } from '../agent-types';
import { AgentModelSelect } from '../components/AgentModelSelect';
import { AgentToolSelects } from '../components/AgentToolSelects';
import { dirtyFields } from '../components/DirectoryEditor';

const empty:AgentDraft={name:'',code:'',description:'',enabled:false,model_id:0,vision_model_id:null,fallback_model_id:null,system_prompt:'',personality_prompt:'',tools:[],skill_ids:[],mcp_ids:[],options:{}};

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
      <div><label>角色<textarea rows={14} value={draft.system_prompt}
        onChange={e=>set(previous=>({...previous,system_prompt:e.target.value}))}/></label>
        <p className="field-help">定义职责、目标和行为规则。</p></div>
      <div><label>性格<textarea rows={14} value={draft.personality_prompt ?? ''}
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
export function Agents({ token }: { token: string }) {
  const api = useMemo(() => new AgentApi(token), [token]);
  const [items, setItems] = useState<Agent[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, AgentEdit>>({});
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const pending = useRef<AbortController>();
  const selectedRef = useRef(selected);
  selectedRef.current = selected;
  const alive = useRef(true);
  const errorRef = useRef<HTMLDivElement>(null);

  /** 刷新只替换目录；已经编辑的草稿仍绑定原快照，不能绕过乐观锁覆盖外部修改。 */
  const load = useCallback(async (after?: string) => {
    pending.current?.abort();
    const controller = new AbortController();
    pending.current = controller;
    setLoading(true); setError('');
    try {
      const page = await api.listAgents(after, controller.signal);
      if (controller.signal.aborted) return;
      const current = selectedRef.current;
      // 当前 Agent 可能在后续分页；刷新首屏时单独读取它，避免编辑区突然消失。
      if (!after && current && current !== 'new'
        && !page.items.some(agent => String(agent.id) === current)) {
        const active = await api.getAgent(Number(current), controller.signal);
        if (controller.signal.aborted) return;
        page.items = [...page.items, active];
      }
      setItems(previous => after
        ? [...previous, ...page.items.filter(item => !previous.some(old => old.id === item.id))]
        : page.items);
      setCursor(page.next_cursor);
      setSelected(previous => previous ?? (page.items[0] ? String(page.items[0].id) : null));
      if (!after) setRefresh(value => value + 1);
    } catch (failure) {
      if (!controller.signal.aborted) setError(errorMessage(failure));
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }, [api]);

  useEffect(() => {
    alive.current = true;
    void load();
    // 页面离开或登录凭据更新时取消尚未结束的目录查询。
    return () => { alive.current = false; pending.current?.abort(); };
  }, [load]);
  useEffect(() => {
    // 长页面保存失败后将焦点移到错误，使浏览器滚入可见区域。
    if (error) errorRef.current?.focus();
  }, [error]);

  const item = items.find(agent => String(agent.id) === selected);
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

  /** 保存只提交已编辑字段，保留隐藏的 Skill 与未更改的其他权限。 */
  async function save() {
    if (!draft || !selected || busy || promptError(draft)) return;
    const key = selected;
    // 写入优先于目录读取；即使旧响应忽略取消并晚到，也不能回填过时快照。
    pending.current?.abort(); setLoading(false);
    setBusy(true); setError(''); setNotice('');
    try {
      const changes = original ? dirtyFields(editableAgent(original), draft) : draft;
      if (original && !Object.keys(changes).length) {
        setNotice('没有需要保存的修改。');
        return;
      }
      const saved = original
        ? await api.updateAgent(original.id, original.revision, changes)
        : await api.createAgent(draft);
      if (!alive.current) return;
      setItems(previous => previous.some(agent => agent.id === saved.id)
        ? previous.map(agent => agent.id === saved.id ? saved : agent) : [...previous, saved]);
      setDrafts(previous => { const next = { ...previous }; delete next[key]; return next; });
      setSelected(String(saved.id)); setNotice('Agent 已保存。'); setConfirmDelete(false);
    } catch (failure) {
      if (alive.current) setError(errorMessage(failure));
    } finally { if (alive.current) setBusy(false); }
  }

  /** 撤销仅丢弃当前身份的草稿；取消新建后回到已有 Agent。 */
  function discard() {
    if (!selected) return;
    setDrafts(previous => { const next = { ...previous }; delete next[selected]; return next; });
    if (selected === 'new') setSelected(items[0] ? String(items[0].id) : null);
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
      setItems(remaining);
      setDrafts(previous => { const next = { ...previous }; delete next[String(id)]; return next; });
      setSelected(remaining[0] ? String(remaining[0].id) : drafts.new ? 'new' : null);
      setConfirmDelete(false); setNotice('Agent 已删除。');
    } catch (failure) {
      if (alive.current) setError(errorMessage(failure));
    } finally { if (alive.current) setBusy(false); }
  }

  return <div className="page-content agent-page">
    <div className="page-heading"><div><p className="eyebrow">AGENT WORKSPACE</p>
      <h1>角色和人格</h1><p>选择 Agent，在页面中配置角色与性格。</p></div>
      <div className="heading-actions"><Button disabled={busy || loading} onClick={create}>新增 Agent</Button>
        <Button disabled={busy} loading={loading} onClick={() => void load()}>刷新</Button></div>
    </div>
    <div className="agent-switcher" role="group" aria-label="切换 Agent">
      {items.map(agent => <Button key={agent.id} type={selected === String(agent.id) ? 'primary' : 'default'}
        aria-label={agent.name} aria-pressed={selected === String(agent.id)} disabled={busy || loading}
        onClick={() => selectAgent(String(agent.id))}>{agent.name}</Button>)}
      {drafts.new && <Button type={selected === 'new' ? 'primary' : 'default'} aria-pressed={selected === 'new'}
        aria-label={drafts.new.value.name || '新 Agent'} disabled={busy || loading}
        onClick={() => selectAgent('new')}>{drafts.new.value.name || '新 Agent'}</Button>}
      {cursor && <Button disabled={busy} loading={loading} onClick={() => void load(cursor)}>加载更多 Agent</Button>}
    </div>
    {error && <div ref={errorRef} role="group" aria-label="操作错误" tabIndex={-1} className="notice">
      <Alert type="error" showIcon message={error}/></div>}
    {notice && <Alert type="success" showIcon className="notice" message={notice}/>}
    {!draft && <div className="panel empty-list">{loading ? '正在加载…' : '暂无 Agent，点击“新增 Agent”开始配置。'}</div>}
    {draft && <form className="catalog-form panel agent-editor" aria-label="Agent 配置"
      onSubmit={event => { event.preventDefault(); void save(); }}>
      <fieldset className="agent-editor-fields" disabled={busy}>
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
        <AgentPromptFields draft={draft} set={setDraft}/>
        <div className="heading-actions agent-save-actions">
          <Button type="primary" htmlType="submit" loading={busy} disabled={busy || Boolean(promptError(draft))}>保存 Agent</Button>
          <Button disabled={busy} onClick={discard}>{selected === 'new' ? '取消新建' : '撤销修改'}</Button>
          {original && <Button danger disabled={busy || original.enabled || draft.enabled}
            onClick={() => setConfirmDelete(true)}>删除 Agent</Button>}
        </div>
        {confirmDelete && original && <div className="agent-delete-confirm" role="group" aria-label="删除确认">
          <p>确定删除“{original.name}”？此操作也会丢弃当前 Agent 的未保存修改。</p>
          <Button danger disabled={busy} onClick={() => void remove()}>确认删除</Button>
          <Button disabled={busy} onClick={() => setConfirmDelete(false)}>取消删除</Button>
        </div>}
        <AgentModelSelect key={`${selected}-${refresh}`} token={token} value={draft.model_id}
          visionValue={draft.vision_model_id ?? null} fallbackValue={draft.fallback_model_id ?? null}
          onChange={model_id => setDraft(previous => ({ ...previous, model_id }))}
          onVisionChange={vision_model_id => setDraft(previous => ({ ...previous, vision_model_id }))}
          onFallbackChange={fallback_model_id => setDraft(previous => ({ ...previous, fallback_model_id }))}/>
        <Alert type="info" message="工具调用次数在“机器人配置 → LLM设置”中统一调整。"/>
        <AgentToolSelects key={`tools-${selected}-${refresh}`} token={token} tools={draft.tools} mcpIds={draft.mcp_ids}
          onToolsChange={tools => setDraft(previous => ({ ...previous, tools }))}
          onMcpsChange={mcp_ids => setDraft(previous => ({ ...previous, mcp_ids }))}/>
      </fieldset>
    </form>}
  </div>;
}
