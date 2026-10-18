import { Alert, Button, Select, Tag } from 'antd';
import { useEffect, useMemo, useRef, useState } from 'react';
import { errorMessage } from '../api';
import { KnowledgeApi } from '../knowledge-api';
import type { MemoryFilters, MemoryScope, MemoryScopeInfo, TagCount, TaggedEntry, TaggedPage } from '../knowledge-types';
import { CatalogDialog } from './CatalogDialog';
import { DetailDialog } from './DetailDialog';

const scopeNames: Record<MemoryScope, string> = {global: '全局', session: '会话', user: '用户', group: '群聊'};
const emptyFilters: MemoryFilters = {query: '', tag: '', scope: '', scope_id: ''};
const emptyDraft = {title: '', key: '', content: '', tags: [] as string[], scope: 'user' as MemoryScope,
  source: 'new' as 'new' | 'existing', scope_id: '', bot_id: '', target: ''};

/** 知识标题与记忆键共同用于只读详情及删除目标。 */
function recordName(item: TaggedEntry) { return 'title' in item ? item.title : item.key; }

/** 两类记录共用分页、取消和写入收尾；表单按对象区分身份字段。 */
export function KnowledgeRecords({token, kind}: {token: string; kind: 'knowledge' | 'memory'}) {
  const memory = kind === 'memory', label = memory ? '记忆' : '知识';
  const api = useMemo(() => new KnowledgeApi(token), [token]);
  const [filters, setFilters] = useState<MemoryFilters>(emptyFilters);
  const [filterDraft, setFilterDraft] = useState<MemoryFilters>(emptyFilters);
  const [page, setPage] = useState<TaggedPage>({items: [], next_cursor: null});
  const [tags, setTags] = useState<TagCount[]>([]), [scopes, setScopes] = useState<MemoryScopeInfo[]>([]);
  const [cursor, setCursor] = useState<string>(), [history, setHistory] = useState<(string | undefined)[]>([]);
  const [loading, setLoading] = useState(true), [error, setError] = useState(''), [refresh, setRefresh] = useState(0);
  const [adding, setAdding] = useState(false), [draft, setDraft] = useState(emptyDraft);
  const [saving, setSaving] = useState(false), [saveError, setSaveError] = useState('');
  const [selected, setSelected] = useState<number>(), [detail, setDetail] = useState<TaggedEntry>();
  const [detailLoading, setDetailLoading] = useState(false), [detailError, setDetailError] = useState('');
  const [deleting, setDeleting] = useState<TaggedEntry>(), [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState('');
  const detailRequest = useRef<AbortController>();
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; detailRequest.current?.abort(); };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError(''); detailRequest.current?.abort(); setSelected(undefined); setDetail(undefined);
    /** 同一代请求读取列表和目录，过时响应不能覆盖新筛选。 */
    async function load() {
      try {
        const [records, catalog, owners] = await Promise.all([
          memory ? api.listMemories(filters, cursor, controller.signal) : api.listKnowledge(filters, cursor, controller.signal),
          memory ? api.memoryTags(filters, controller.signal) : api.knowledgeTags(controller.signal),
          memory ? api.memoryScopes(controller.signal) : Promise.resolve([] as MemoryScopeInfo[]),
        ]);
        if (!controller.signal.aborted) { setPage(records); setTags(catalog); setScopes(owners); }
      } catch (failure) { if (!controller.signal.aborted) setError(errorMessage(failure)); }
      finally { if (!controller.signal.aborted) setLoading(false); }
    }
    void load();
    return () => { controller.abort(); detailRequest.current?.abort(); };
  }, [api, memory, filters, cursor, refresh]);

  /** 新增重新建立草稿，不从已有记录填入可编辑数据。 */
  function beginAdd() {
    setDraft({...emptyDraft, tags: [], source: memory && scopes.length ? 'existing' : 'new',
      scope_id: memory && scopes.length ? scopes[0].scope_id : '',
      scope: memory && scopes.length ? scopes[0].scope : 'user'});
    setSaveError(''); setAdding(true);
  }

  /** 写请求完成后才关闭弹窗，失败保留全部草稿，不尝试更新旧记录。 */
  async function save() {
    if (saving) return;
    setSaving(true); setSaveError('');
    try {
      if (memory) {
        await api.createMemory({scope: draft.scope, key: draft.key, content: draft.content, tags: draft.tags,
          ...(draft.source === 'existing' ? {scope_id: draft.scope_id} : {bot_id: draft.bot_id,
            ...(draft.scope === 'global' ? {} : {target: draft.target})})});
      } else await api.createKnowledge({title: draft.title, content: draft.content, tags: draft.tags});
      if (mounted.current) { setAdding(false); setCursor(undefined); setHistory([]); setRefresh(value => value + 1); }
    } catch (failure) { if (mounted.current) setSaveError(errorMessage(failure)); }
    finally { if (mounted.current) setSaving(false); }
  }

  /** 关闭详情立即取消，响应即使晚到也不能重新打开。 */
  function closeDetail() { detailRequest.current?.abort(); setSelected(undefined); setDetail(undefined); }

  /** 先打开详情框，再读取完整正文，避免列表摘要被当作完整资料。 */
  async function openDetail(id: number) {
    detailRequest.current?.abort();
    const controller = new AbortController(); detailRequest.current = controller;
    setSelected(id); setDetail(undefined); setDetailLoading(true); setDetailError('');
    try {
      const value = memory ? await api.getMemory(id, controller.signal) : await api.getKnowledge(id, controller.signal);
      if (!controller.signal.aborted) setDetail(value);
    } catch (failure) { if (!controller.signal.aborted) setDetailError(errorMessage(failure)); }
    finally { if (!controller.signal.aborted) setDetailLoading(false); }
  }

  /** 仅明确确认后删除；失败保持确认框和目标，不能假装删除成功。 */
  async function remove() {
    if (!deleting || deleteBusy) return;
    setDeleteBusy(true); setDeleteError('');
    try {
      if (memory) await api.deleteMemory(deleting.id); else await api.deleteKnowledge(deleting.id);
      if (mounted.current) {
        setDeleting(undefined); closeDetail(); setCursor(undefined); setHistory([]); setRefresh(value => value + 1);
      }
    } catch (failure) { if (mounted.current) setDeleteError(errorMessage(failure)); }
    finally { if (mounted.current) setDeleteBusy(false); }
  }

  const targetLabel = draft.scope === 'user' ? '用户 QQ 号' : draft.scope === 'group' ? '群号' : '会话 ID';
  const validIdentity = draft.source === 'existing' ? !!draft.scope_id : !!draft.bot_id.trim() && (draft.scope === 'global' || !!draft.target.trim());
  return <div className="page-content">
    <div className="page-heading"><div><p className="eyebrow">{memory ? 'TAGGED MEMORY' : 'KNOWLEDGE'}</p>
      <h1>{memory ? '标签记忆' : '知识条目'}</h1>
      <p>{memory ? '查看和新增机器人记忆，按用户、群聊或会话整理。已有内容不可编辑。' : '维护机器人可检索的共享资料，使用标签组织主题。已有内容不可编辑。'}</p>
    </div><div className="heading-actions"><Button loading={loading} onClick={() => setRefresh(value => value + 1)}>刷新</Button>
      <Button type="primary" disabled={loading} onClick={beginAdd}>新增{label}</Button></div></div>
    {error && <Alert type="error" showIcon message={error} className="notice" />}
    <form className="record-filter" onSubmit={event => { event.preventDefault(); setCursor(undefined); setHistory([]); setFilters({...filterDraft}); }}>
      <label>关键词<input maxLength={200} value={filterDraft.query} onChange={event => setFilterDraft({...filterDraft, query: event.target.value})} /></label>
      <label>标签<select value={filterDraft.tag} onChange={event => setFilterDraft({...filterDraft, tag: event.target.value})}>
        <option value="">全部标签</option>{tags.map(tag => <option key={tag.tag} value={tag.tag}>{tag.tag}（{tag.count}）</option>)}</select></label>
      {memory && <><label>作用域类型<select value={filterDraft.scope} onChange={event => setFilterDraft({...filterDraft, scope: event.target.value as MemoryScope | '', scope_id: ''})}>
        <option value="">全部类型</option>{Object.entries(scopeNames).map(([value, name]) => <option key={value} value={value}>{name}</option>)}</select></label>
        <label>目标作用域<select value={filterDraft.scope_id} onChange={event => setFilterDraft({...filterDraft, scope_id: event.target.value})}>
          <option value="">全部作用域</option>{scopes.filter(scope => !filterDraft.scope || scope.scope === filterDraft.scope).map(scope =>
            <option key={scope.scope_id} value={scope.scope_id}>{scopeNames[scope.scope]} · {scope.scope_id.slice(0, 12)}…</option>)}</select></label></>}
      <Button aria-label="筛选" htmlType="submit" disabled={loading}>筛选</Button>
    </form>
    <div className="panel table-wrap"><table className="data-table"><thead><tr><th>{memory ? '记忆键 / 归属' : '标题'}</th><th>正文摘要</th><th>标签</th><th>创建时间</th><th>操作</th></tr></thead><tbody>
      {page.items.map(item => <tr key={item.id}><td><strong>{recordName(item)}</strong>{'scope' in item && <small title={item.scope_id}>{scopeNames[item.scope]} · {item.scope_id.slice(0, 12)}…</small>}</td>
        <td className="knowledge-summary">{item.content.slice(0, 100)}{item.content.length > 100 && '…'}</td>
        <td>{item.tags.map(tag => <Tag key={tag}>{tag}</Tag>)}</td><td>{item.created_at.replace('T', ' ')}</td>
        <td><div className="heading-actions"><Button aria-label={'查看' + recordName(item)} disabled={loading} onClick={() => void openDetail(item.id)}>查看</Button>
          <Button danger aria-label={'删除' + recordName(item)} disabled={loading} onClick={() => { setDeleteError(''); setDeleting(item); }}>删除</Button></div></td></tr>)}
    </tbody></table>{!page.items.length && <p className="empty-list">{loading ? '正在加载…' : `暂无符合条件的${label}。`}</p>}</div>
    <div className="catalog-pagination"><Button disabled={loading || !history.length} onClick={() => { setCursor(history.at(-1)); setHistory(history.slice(0, -1)); }}>上一页</Button>
      <Button disabled={loading || !page.next_cursor} onClick={() => { setHistory([...history, cursor]); setCursor(page.next_cursor!); }}>下一页</Button></div>
    {adding && <CatalogDialog title={'新增' + label} busy={saving} error={saveError} onCancel={() => setAdding(false)}>
      <form className="catalog-form knowledge-form" onSubmit={event => { event.preventDefault(); void save(); }}>
        {memory ? <>
          <label>作用域来源<select disabled={saving} value={draft.source} onChange={event => setDraft({...draft, source: event.target.value as 'new' | 'existing', scope_id: ''})}>
            <option value="new">指定新作用域</option><option value="existing" disabled={!scopes.length}>使用已有作用域</option></select></label>
          {draft.source === 'existing' ? <label>已有作用域<select required disabled={saving} value={draft.scope_id} onChange={event => {
            const owner = scopes.find(scope => scope.scope_id === event.target.value);
            setDraft({...draft, scope_id: event.target.value, scope: owner?.scope ?? draft.scope});
          }}><option value="">请选择</option>{scopes.map(scope => <option key={scope.scope_id} value={scope.scope_id}>{scopeNames[scope.scope]} · {scope.scope_id.slice(0, 12)}…（{scope.count}）</option>)}</select></label>
            : <><label>作用域<select disabled={saving} value={draft.scope} onChange={event => setDraft({...draft, scope: event.target.value as MemoryScope, target: ''})}>
              {Object.entries(scopeNames).map(([value, name]) => <option key={value} value={value}>{name}</option>)}</select></label>
              <label>机器人 QQ 号<input required disabled={saving} maxLength={20} pattern="[1-9][0-9]{0,19}" value={draft.bot_id} onChange={event => setDraft({...draft, bot_id: event.target.value})} /></label>
              {draft.scope !== 'global' && <label>{targetLabel}<input required disabled={saving} maxLength={draft.scope === 'session' ? 35 : 20}
                pattern={draft.scope === 'session' ? 'qq_[a-f0-9]{32}' : '[1-9][0-9]{0,19}'} value={draft.target}
                placeholder={draft.scope === 'session' ? '填写会话记录中的 qq_ 会话 ID' : undefined}
                onChange={event => setDraft({...draft, target: event.target.value})} /></label>}</>}
          <label>记忆键<input required disabled={saving} maxLength={80} value={draft.key} onChange={event => setDraft({...draft, key: event.target.value})} /></label>
          <p>已有记忆键不能覆盖或恢复，新增事实请使用新键。</p>
        </> : <label>知识标题<input required disabled={saving} maxLength={200} value={draft.title} onChange={event => setDraft({...draft, title: event.target.value})} /></label>}
        <label>正文<textarea required disabled={saving} rows={8} maxLength={memory ? 1000 : 16000} value={draft.content} onChange={event => setDraft({...draft, content: event.target.value})} /></label>
        <label htmlFor="knowledge-entry-tags">主题标签</label>
        <Select id="knowledge-entry-tags" aria-label="主题标签" mode="tags" disabled={saving} value={draft.tags} tokenSeparators={[',', '，']}
          options={tags.map(tag => ({value: tag.tag, label: tag.tag}))} placeholder="输入标签后按回车，最多 8 个"
          onChange={values => setDraft({...draft, tags: values})} style={{width: '100%'}} />
        <p>每个标签最多 32 字符。保存后内容和标签不可编辑。</p>
        <div className="dialog-actions"><Button disabled={saving} onClick={() => setAdding(false)}>取消</Button>
          <Button type="primary" htmlType="submit" loading={saving} disabled={!draft.content.trim() || !draft.tags.length || draft.tags.length > 8 || (memory ? !draft.key.trim() || !validIdentity : !draft.title.trim())}>保存{label}</Button></div>
      </form>
    </CatalogDialog>}
    {selected !== undefined && <DetailDialog title={`${label}详情 #${selected}`} loading={detailLoading} error={detailError} onClose={closeDetail}>
      {detail && <><h3>{recordName(detail)}</h3><p>{detail.tags.map(tag => <Tag key={tag}>{tag}</Tag>)}</p>
        {'scope' in detail && <p className="knowledge-owner">作用域：{scopeNames[detail.scope]} · {detail.scope_id}</p>}
        {'material_ids' in detail && !!detail.material_ids?.length && <p>关联素材：{detail.material_ids.map(id => <Tag key={id}>{id}</Tag>)}</p>}
        <pre className="knowledge-content">{detail.content}</pre><p>创建时间：{detail.created_at.replace('T', ' ')}</p></>}
    </DetailDialog>}
    {deleting && <CatalogDialog title={'删除' + label} busy={deleteBusy} error={deleteError} onCancel={() => setDeleting(undefined)}>
      <p>确认删除“{recordName(deleting)}”？删除后将不再出现在列表和检索结果中。</p>
      <div className="dialog-actions"><Button disabled={deleteBusy} onClick={() => setDeleting(undefined)}>取消</Button>
        <Button danger type="primary" loading={deleteBusy} onClick={() => void remove()}>确认删除</Button></div>
    </CatalogDialog>}
  </div>;
}
