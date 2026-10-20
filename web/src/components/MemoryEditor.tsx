import { Button, Select } from 'antd';
import { useEffect, useRef, useState } from 'react';
import { ApiFailure, errorMessage } from '../api';
import type { KnowledgeApi } from '../knowledge-api';
import type { MemoryEntry, MemoryKind, MemoryScope, MemoryScopeInfo, MemoryUpdate, TagCount } from '../knowledge-types';
import { CatalogDialog } from './CatalogDialog';
import { memoryKindNames, memoryScopeLabel, memoryScopeNames } from './MemoryFacts';

interface Draft {
  key: string; content: string; tags: string[]; topic: string; kind: MemoryKind;
  source: 'new' | 'existing'; scope: MemoryScope; scope_id: string; bot_id: string; target: string;
  valid_from: string; valid_until: string; reason: string;
}

/** 时间输入显示本地时区；未修改的时间完全不写回，保留服务端原始精度。 */
function localTime(value?: string | null) {
  if (!value) return '';
  const date = new Date(value);
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}

/** 读取完整事实后才建立编辑草稿，保存失败保留输入且不自动改用新的版本。 */
export function MemoryEditor({api, id, scopes, tags, onClose, onSaved}: {
  api: KnowledgeApi; id?: number; scopes: MemoryScopeInfo[]; tags: TagCount[];
  onClose: () => void; onSaved: () => void;
}) {
  const [draft, setDraft] = useState<Draft>({key: '', content: '', tags: [], topic: '', kind: 'fact',
    source: scopes.length ? 'existing' : 'new', scope: scopes[0]?.scope || 'user', scope_id: scopes[0]?.scope_id || '',
    bot_id: '', target: '', valid_from: '', valid_until: '', reason: ''});
  const [original, setOriginal] = useState<MemoryEntry>();
  const [loading, setLoading] = useState(id !== undefined), [busy, setBusy] = useState(false), [error, setError] = useState('');
  const [conflict, setConflict] = useState(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    const controller = new AbortController();
    /** 页面关闭后只取消读取；写入已发出时不通过取消伪装成未保存。 */
    async function load() {
      if (id === undefined) return;
      try {
        const value = await api.getMemory(id, controller.signal);
        if (!controller.signal.aborted) {
          setOriginal(value);
          setDraft(previous => ({...previous, key: value.key, content: value.content, tags: value.tags,
            topic: value.topic || '', kind: value.kind, valid_from: localTime(value.valid_from), valid_until: localTime(value.valid_until)}));
        }
      } catch (failure) { if (!controller.signal.aborted) setError(errorMessage(failure)); }
      finally { if (!controller.signal.aborted) setLoading(false); }
    }
    void load();
    return () => { mounted.current = false; controller.abort(); };
  }, [api, id]);

  /** 比较原始草稿，仅携带改动字段；清空有效期显式提交 null。 */
  function changes(): MemoryUpdate {
    const body: MemoryUpdate = {expected_revision: original!.revision, reason: draft.reason.trim()};
    if (draft.content !== original!.content) body.content = draft.content;
    if (draft.topic !== (original!.topic || '')) body.topic = draft.topic;
    if (JSON.stringify(draft.tags) !== JSON.stringify(original!.tags)) body.tags = draft.tags;
    if (draft.valid_from !== localTime(original!.valid_from)) body.valid_from = draft.valid_from ? new Date(draft.valid_from).toISOString() : null;
    if (draft.valid_until !== localTime(original!.valid_until)) body.valid_until = draft.valid_until ? new Date(draft.valid_until).toISOString() : null;
    return body;
  }

  /** 版本冲突后需关闭并重新读取，禁止自动重试覆盖其他人的修订。 */
  async function save() {
    if (busy) return;
    setBusy(true); setError(''); setConflict(false);
    try {
      if (id !== undefined) await api.updateMemory(id, changes());
      else await api.createMemory({scope: draft.scope, key: draft.key, content: draft.content, tags: draft.tags,
        ...(draft.topic.trim() ? {topic: draft.topic} : {}), kind: draft.kind,
        ...(draft.valid_from ? {valid_from: new Date(draft.valid_from).toISOString()} : {}),
        ...(draft.valid_until ? {valid_until: new Date(draft.valid_until).toISOString()} : {}),
        ...(draft.source === 'existing' ? {scope_id: draft.scope_id} : {bot_id: draft.bot_id,
          ...(draft.scope === 'global' ? {} : {target: draft.target})})});
      if (mounted.current) onSaved();
    } catch (failure) {
      if (mounted.current) {
        setError(errorMessage(failure));
        setConflict(failure instanceof ApiFailure && failure.status === 409);
      }
    } finally { if (mounted.current) setBusy(false); }
  }

  const editing = id !== undefined;
  const identityValid = draft.source === 'existing' ? !!draft.scope_id : !!draft.bot_id.trim() && (draft.scope === 'global' || !!draft.target.trim());
  const changed = original && Object.keys(changes()).length > 2;
  const terminal = original?.status === 'deleted' || original?.status === 'merged';
  const valid = !loading && (!editing || !!original) && !terminal && !!draft.content.trim() && draft.tags.length > 0 && draft.tags.length <= 8
    && draft.tags.every(tag => tag.length <= 32) && (editing ? !!draft.reason.trim() && changed
      && (!!draft.topic.trim() || !original?.topic) : !!draft.key.trim() && identityValid);
  return <CatalogDialog title={editing ? '编辑记忆' : '新增记忆'} busy={busy} error={error} onCancel={onClose}>
    {loading ? <p role="status">正在读取完整记忆…</p> : <form className="catalog-form knowledge-form" onSubmit={event => { event.preventDefault(); if (valid) void save(); }}>
      {conflict && <p>草稿已保留，请先核对最新版本再决定修改。不会自动覆盖已有记忆。</p>}
      {!editing && <>
        <label>作用域来源<select disabled={busy} value={draft.source} onChange={event => setDraft({...draft, source: event.target.value as 'new' | 'existing'})}>
          <option value="new">指定新作用域</option><option value="existing" disabled={!scopes.length}>使用已有作用域</option></select></label>
        {draft.source === 'existing' ? <label>已有作用域<select required disabled={busy} value={draft.scope_id} onChange={event => {
          const owner = scopes.find(scope => scope.scope_id === event.target.value);
          setDraft({...draft, scope_id: event.target.value, scope: owner!.scope});
        }}>{scopes.map(scope => <option key={scope.scope_id} value={scope.scope_id}>{memoryScopeLabel(scope)}（{scope.count}）</option>)}</select></label> : <>
          <label>作用域<select disabled={busy} value={draft.scope} onChange={event => setDraft({...draft, scope: event.target.value as MemoryScope, target: ''})}>
            {Object.entries(memoryScopeNames).map(([value, name]) => <option key={value} value={value}>{name}</option>)}</select></label>
          <label>机器人 QQ 号<input required disabled={busy} pattern="[1-9][0-9]{0,19}" maxLength={20} value={draft.bot_id} onChange={event => setDraft({...draft, bot_id: event.target.value})} /></label>
          {draft.scope !== 'global' && <label>{draft.scope === 'user' ? '用户 QQ 号' : draft.scope === 'group' ? '群号' : '会话 ID'}<input required disabled={busy}
            pattern={draft.scope === 'session' ? 'qq_[a-f0-9]{32}' : '[1-9][0-9]{0,19}'} maxLength={draft.scope === 'session' ? 35 : 20}
            value={draft.target} onChange={event => setDraft({...draft, target: event.target.value})} /></label>}
        </>}
        <label>记忆键<input required disabled={busy} maxLength={80} value={draft.key} onChange={event => setDraft({...draft, key: event.target.value})} /></label>
        <label>记忆类型<select disabled={busy} value={draft.kind} onChange={event => setDraft({...draft, kind: event.target.value as MemoryKind})}>
          {Object.entries(memoryKindNames).map(([value, name]) => <option key={value} value={value}>{name}</option>)}</select></label>
      </>}
      {editing && original && <p>当前编辑“{original.key}”的第 {original.revision} 版，归属：{memoryScopeLabel(original)}。</p>}
      {terminal && <p>已遗忘或合并的记忆不能从编辑界面恢复。</p>}
      <label>记忆主题<input disabled={busy || terminal} maxLength={80} value={draft.topic} onChange={event => setDraft({...draft, topic: event.target.value})} /></label>
      <label>正文<textarea required disabled={busy || terminal} rows={8} maxLength={1000} value={draft.content} onChange={event => setDraft({...draft, content: event.target.value})} /></label>
      <label htmlFor="memory-entry-tags">主题标签</label>
      <Select id="memory-entry-tags" aria-label="主题标签" mode="tags" disabled={busy || terminal} value={draft.tags} tokenSeparators={[',', '，']}
        options={tags.map(tag => ({value: tag.tag, label: tag.tag}))} placeholder="输入标签后按回车，最多 8 个"
        onChange={values => setDraft({...draft, tags: values})} style={{width: '100%'}} />
      <label>生效时间<input type="datetime-local" disabled={busy || terminal} value={draft.valid_from} onChange={event => setDraft({...draft, valid_from: event.target.value})} /></label>
      <label>失效时间<input type="datetime-local" disabled={busy || terminal} value={draft.valid_until} onChange={event => setDraft({...draft, valid_until: event.target.value})} /></label>
      <p>时间按当前设备时区输入。未设置时间不代表事实永久可信；来源与事实时间请在详情中核对。</p>
      {editing && <label>修改原因<textarea required maxLength={300} disabled={busy || terminal} value={draft.reason} onChange={event => setDraft({...draft, reason: event.target.value})} /></label>}
      <div className="dialog-actions"><Button disabled={busy} onClick={onClose}>取消</Button>
        <Button type="primary" htmlType="submit" loading={busy} disabled={!valid}>{editing ? '保存修改' : '保存记忆'}</Button></div>
    </form>}
  </CatalogDialog>;
}
