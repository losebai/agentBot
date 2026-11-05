import { ChoiceSelect } from './ChoiceSelect';
import { Alert, Button, Tag } from 'antd';
import { useEffect, useMemo, useRef, useState } from 'react';
import type { CatalogPage } from '../ai-types';
import { errorMessage } from '../api';
import { KnowledgeApi } from '../knowledge-api';
import type { MemoryEntry, MemoryFilters, MemoryKind, MemoryScope, MemoryScopeInfo, MemoryStatus, TagCount } from '../knowledge-types';
import { CatalogDialog } from './CatalogDialog';
import { MemoryBatchDialog, type MemoryBatch } from './MemoryBatchDialog';
import { MemoryDetail } from './MemoryDetail';
import { MemoryEditor } from './MemoryEditor';
import { memoryKindNames, memoryScopeLabel, memoryScopeNames, memorySourceNames, memoryStatusNames } from './MemoryFacts';
import { RecordPagination } from './RecordPagination';

const emptyFilters: MemoryFilters = {query: '', tag: '', scope: '', scope_id: '', topic: '', kind: '', status: ''};

/** 记忆页管理事实生命周期，与只读知识条目保持独立。 */
export function TaggedMemories({token}: {token: string}) {
  const api = useMemo(() => new KnowledgeApi(token), [token]);
  const [filters, setFilters] = useState<MemoryFilters>(emptyFilters), [filterDraft, setFilterDraft] = useState<MemoryFilters>(emptyFilters);
  const [page, setPage] = useState<CatalogPage<MemoryEntry>>({items: [], next_cursor: null, total: 0});
  const [tags, setTags] = useState<TagCount[]>([]), [scopes, setScopes] = useState<MemoryScopeInfo[]>([]);
  const [pageNumber, setPageNumber] = useState(1), [pageSize, setPageSize] = useState(10);
  const [loading, setLoading] = useState(true), [error, setError] = useState(''), [refresh, setRefresh] = useState(0);
  const [detail, setDetail] = useState<number>(), [editor, setEditor] = useState<{id?: number}>();
  const [checked, setChecked] = useState<number[]>([]), [batch, setBatch] = useState<MemoryBatch>();
  const [cleanupAction, setCleanupAction] = useState<'expire' | 'forget'>('expire');
  const [deleting, setDeleting] = useState<MemoryEntry>(), [deleteBusy, setDeleteBusy] = useState(false), [deleteError, setDeleteError] = useState('');
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError(''); setChecked([]); setDetail(undefined);
    /** 同一代列表、标签与作用域共享取消信号，旧响应不能覆盖新的筛选。 */
    async function load() {
      try {
        const [records, catalog, owners] = await Promise.all([
          api.listMemories(filters, undefined, controller.signal, {page: pageNumber, limit: pageSize}), api.memoryTags(filters, controller.signal), api.memoryScopes(controller.signal),
        ]);
        if (!controller.signal.aborted) { setPage(records); setTags(catalog); setScopes(owners); const last=Math.max(1,Math.ceil(records.total/pageSize)); if(pageNumber>last)setPageNumber(last); }
      } catch (failure) { if (!controller.signal.aborted) setError(errorMessage(failure)); }
      finally { if (!controller.signal.aborted) setLoading(false); }
    }
    void load();
    return () => controller.abort();
  }, [api, filters, pageNumber, pageSize, refresh]);

  /** 写操作成功后回第一页读取当前快照，不在客户端推测新的版本或来源。 */
  function saved() {
    setEditor(undefined); setBatch(undefined); setDeleting(undefined); setDetail(undefined);
    setPageNumber(1); setChecked([]); setRefresh(value => value + 1);
  }

  /** 遗忘只提交看到的版本，冲突或失败时保持确认框并展示服务端原因。 */
  async function forget() {
    if (!deleting || deleteBusy) return;
    setDeleteBusy(true); setDeleteError('');
    try {
      await api.deleteMemory(deleting.id, deleting.revision);
      if (mounted.current) saved();
    } catch (failure) { if (mounted.current) setDeleteError(errorMessage(failure)); }
    finally { if (mounted.current) setDeleteBusy(false); }
  }

  const selected = page.items.filter(item => checked.includes(item.id));
  return <div className="page-content">
    <div className="page-heading"><div><p className="eyebrow">MEMORY</p><h1>标签记忆</h1>
      <p>按主题整理记忆，核对来源与有效期，修订时保留版本历史。群友观点不等于本人的长期偏好。</p>
    </div><div className="heading-actions"><Button loading={loading} onClick={() => setRefresh(value => value + 1)}>刷新</Button>
      <Button type="primary" disabled={loading} onClick={() => setEditor({})}>新增记忆</Button></div></div>
    {error && <Alert type="error" showIcon message={error} className="notice" />}
    <form className="record-filter" onSubmit={event => { event.preventDefault(); setPageNumber(1); setFilters({...filterDraft}); }}>
      <label>关键词<input maxLength={200} value={filterDraft.query} onChange={event => setFilterDraft({...filterDraft, query: event.target.value})} /></label>
      <label>标签<ChoiceSelect aria-label="标签" value={filterDraft.tag} onChange={event => setFilterDraft({...filterDraft, tag: event})}>
        <option value="">全部标签</option>{tags.map(tag => <option key={tag.tag} value={tag.tag}>{tag.tag}（{tag.count}）</option>)}</ChoiceSelect></label>
      <label>主题<input maxLength={80} value={filterDraft.topic} onChange={event => setFilterDraft({...filterDraft, topic: event.target.value})} /></label>
      <label>记忆类型<ChoiceSelect aria-label="记忆类型" value={filterDraft.kind} onChange={event => setFilterDraft({...filterDraft, kind: event as MemoryKind | ''})}>
        <option value="">全部类型</option>{Object.entries(memoryKindNames).map(([value, name]) => <option key={value} value={value}>{name}</option>)}</ChoiceSelect></label>
      <label>状态<ChoiceSelect aria-label="状态" value={filterDraft.status} onChange={event => setFilterDraft({...filterDraft, status: event as MemoryStatus | 'future' | ''})}>
        <option value="">当前有效（默认）</option>{Object.entries(memoryStatusNames).map(([value, name]) => <option key={value} value={value}>{name}</option>)}</ChoiceSelect></label>
      <label>作用域类型<ChoiceSelect aria-label="作用域类型" value={filterDraft.scope} onChange={event => setFilterDraft({...filterDraft, scope: event as MemoryScope | '', scope_id: ''})}>
        <option value="">全部作用域类型</option>{Object.entries(memoryScopeNames).map(([value, name]) => <option key={value} value={value}>{name}</option>)}</ChoiceSelect></label>
      <label>目标作用域<ChoiceSelect aria-label="目标作用域" value={filterDraft.scope_id} onChange={event => setFilterDraft({...filterDraft, scope_id: event})}>
        <option value="">全部作用域</option>{scopes.filter(scope => !filterDraft.scope || scope.scope === filterDraft.scope).map(scope => <option key={scope.scope_id} value={scope.scope_id}>{memoryScopeLabel(scope)}</option>)}</ChoiceSelect></label>
      <Button aria-label="筛选" htmlType="submit" disabled={loading}>筛选</Button>
    </form>
    <div className="heading-actions" style={{marginBottom: 16, flexWrap: 'wrap'}}>
      <Button disabled={loading || selected.length < 2 || selected.length > 8} onClick={() => setBatch({action: 'merge', items: selected})}>合并所选记忆</Button>
      {!!selected.length && <small>已选 {selected.length} 条，单次合并 2 至 8 条。</small>}
      <label>清理方式 <ChoiceSelect aria-label="清理方式" value={cleanupAction} onChange={event => setCleanupAction(event as 'expire' | 'forget')}>
        <option value="expire">标为失效</option><option value="forget">遗忘</option></ChoiceSelect></label>
      <Button disabled={loading || !filterDraft.scope_id || !filterDraft.topic?.trim()} onClick={() => setBatch({
        action: cleanupAction, scope_id: filterDraft.scope_id!, topic: filterDraft.topic!.trim(),
      })}>预览主题清理</Button>
      <small>主题清理需指定目标作用域和主题，确认前会显示影响条目。</small>
    </div>
    <div className="panel table-wrap"><table className="data-table"><thead><tr>
      <th>选择</th><th>记忆键 / 归属</th><th>正文摘要 / 主题</th><th>状态 / 版本 / 来源</th><th>事实时间 / 最近使用</th><th>标签</th><th>操作</th>
    </tr></thead><tbody>{page.items.map(item => {
      const terminal = item.status === 'deleted' || item.status === 'merged';
      return <tr key={item.id}>
        <td><input type="checkbox" aria-label={'选择' + item.key} disabled={loading || terminal} checked={checked.includes(item.id)}
          onChange={event => setChecked(event.target.checked ? [...checked, item.id] : checked.filter(id => id !== item.id))} /></td>
        <td><strong>{item.key}</strong><small title={item.scope_id}>{memoryScopeLabel(item)}</small></td>
        <td className="knowledge-summary">{item.content.slice(0, 100)}{item.content.length > 100 && '…'}<small>{item.topic || '未设置主题'} · {memoryKindNames[item.kind] || '事实'}</small></td>
        <td>{memoryStatusNames[item.effective_status || item.status] || '未知'}<small>第 {item.revision ?? '未知'} 版 · {memorySourceNames[item.source_kind] || '来源未知'}</small></td>
        <td>{item.observed_at?.replace('T', ' ') || '事实时间未知'}<small>{item.last_used_at ? `最近使用：${item.last_used_at.replace('T', ' ')}` : '未记录使用时间'}</small></td>
        <td>{item.tags.map(tag => <Tag key={tag}>{tag}</Tag>)}</td>
        <td><div className="heading-actions">
          <Button aria-label={'查看' + item.key} disabled={loading} onClick={() => setDetail(item.id)}>查看</Button>
          <Button aria-label={'编辑' + item.key} disabled={loading || terminal} onClick={() => setEditor({id: item.id})}>编辑</Button>
          <Button danger aria-label={'遗忘' + item.key} disabled={loading || terminal} onClick={() => { setDeleteError(''); setDeleting(item); }}>遗忘</Button>
        </div></td>
      </tr>;
    })}</tbody></table>{!page.items.length && <p className="empty-list">{loading ? '正在加载…' : '暂无符合条件的记忆。'}</p>}</div>
    <RecordPagination current={pageNumber} pageSize={pageSize} total={page.total} loading={loading}
      hasNext={Boolean(page.next_cursor)} onChange={(number,size)=>{setPageNumber(number);setPageSize(size);}}/>
    {editor && <MemoryEditor api={api} id={editor.id} scopes={scopes} tags={tags} onClose={() => setEditor(undefined)} onSaved={saved} />}
    {detail !== undefined && <MemoryDetail key={detail} api={api} id={detail} onClose={() => setDetail(undefined)} onChanged={saved} />}
    {batch && <MemoryBatchDialog api={api} batch={batch} onClose={() => setBatch(undefined)} onSaved={saved} />}
    {deleting && <CatalogDialog title="遗忘记忆" busy={deleteBusy} error={deleteError} onCancel={() => setDeleting(undefined)}>
      <p>确认遗忘“{deleting.key}”的第 {deleting.revision} 版？遗忘后停止长期记忆召回，仍保留管理员审计历史。本轮不提供恢复功能。</p>
      <div className="dialog-actions"><Button disabled={deleteBusy} onClick={() => setDeleting(undefined)}>取消</Button>
        <Button type="primary" danger loading={deleteBusy} onClick={() => void forget()}>确认遗忘</Button></div>
    </CatalogDialog>}
  </div>;
}
