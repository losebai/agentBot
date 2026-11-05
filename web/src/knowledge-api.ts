import { ApiClient } from './api';
import type { CatalogPage, Page } from './ai-types';
import type { KnowledgeDraft, KnowledgeEntry, KnowledgeFilters, MemoryDraft, MemoryEntry, MemoryFilters, MemoryMerge, MemoryPreview, MemoryResolve, MemoryRevision, MemoryScopeInfo, MemoryUpdate, MemoryVersion, TagCount } from './knowledge-types';

/** 省略空筛选，分页游标始终携带当前提交的条件。 */
function queryString(filters: MemoryFilters = {}, cursor?: string) {
  const params = new URLSearchParams({limit: '10'});
  for (const [key, value] of Object.entries(filters)) if (value) params.set(key, value);
  if (cursor) params.set('cursor', cursor);
  return params.toString();
}

/** 知识允许独立统计与直接跳页；记忆写入携带版本，批量操作必须先预览。 */
export class KnowledgeApi extends ApiClient {
  /** 页码参数只用于知识管理，保留原有 cursor 调用协议。 */
  listKnowledge(filters: KnowledgeFilters = {}, cursor?: string, signal?: AbortSignal,
    pagination?: {page: number; limit: number}) {
    const params = new URLSearchParams(queryString(filters, cursor));
    if (pagination) { params.set('page', String(pagination.page)); params.set('limit', String(pagination.limit)); }
    return this.request<Page<KnowledgeEntry>>(`knowledge?${params}`, 'GET', undefined, signal);
  }
  /** 统计不依赖当前页或目录，翻页时可复用相同筛选的总数。 */
  countKnowledge(filters: KnowledgeFilters = {}, signal?: AbortSignal) {
    const params = new URLSearchParams(queryString(filters)); params.delete('limit');
    return this.request<{total: number}>(`knowledge/count?${params}`, 'GET', undefined, signal);
  }
  /** 详情取消不会改变持久化数据。 */
  getKnowledge(id: number, signal?: AbortSignal) { return this.request<KnowledgeEntry>(`knowledge/${id}`, 'GET', undefined, signal); }
  /** 新增正文与标签在后端同一事务提交。 */
  createKnowledge(body: KnowledgeDraft) { return this.request<KnowledgeEntry>('knowledge', 'POST', body); }
  /** 删除保留原文，由用户确认后调用。 */
  deleteKnowledge(id: number) { return this.request<void>(`knowledge/${id}`, 'DELETE'); }
  /** 标签计数仅包含未删除的知识。 */
  knowledgeTags(signal?: AbortSignal) { return this.request<TagCount[]>('knowledge-tags', 'GET', undefined, signal); }
  /** 管理员查询匿名作用域，不改变 QQ 工具授权。 */
  listMemories(filters: MemoryFilters = {}, cursor?: string, signal?: AbortSignal,
    pagination?: {page: number; limit: number}) {
    const params = new URLSearchParams(queryString(filters, cursor));
    if (pagination) { params.set('page', String(pagination.page)); params.set('limit', String(pagination.limit)); }
    return this.request<CatalogPage<MemoryEntry>>(`memories?${params}`, 'GET', undefined, signal);
  }
  /** 读取完整事实供详情或编辑使用，不从列表摘要建立草稿。 */
  getMemory(id: number, signal?: AbortSignal) { return this.request<MemoryEntry>(`memories/${id}`, 'GET', undefined, signal); }
  /** 新增由服务端执行去重，不能恢复已经遗忘的事实。 */
  createMemory(body: MemoryDraft) { return this.request<MemoryEntry>('memories', 'POST', body); }
  /** 仅提交编辑过的字段，避免旧表单覆盖新的无关修改。 */
  updateMemory(id: number, body: MemoryUpdate) { return this.request<MemoryEntry>(`memories/${id}`, 'PATCH', body); }
  /** 历史读取可取消，版本正文只用于追溯，不提供恢复遗忘入口。 */
  memoryRevisions(id: number, page = 1, limit = 10, signal?: AbortSignal) {
    const params = new URLSearchParams({page: String(page), limit: String(limit)});
    return this.request<CatalogPage<MemoryRevision>>(`memories/${id}/revisions?${params}`, 'GET', undefined, signal);
  }
  /** 冲突必须读取整组候选，不能只修订其中一条而遗漏其他有效结论。 */
  memoryConflicts(id: number, signal?: AbortSignal) {
    return this.request<Page<MemoryEntry>>(`memories/${id}/conflicts`, 'GET', undefined, signal);
  }
  /** 采用已核对的某一结论，409 后不自动重试或刷新草稿版本。 */
  resolveMemories(body: MemoryResolve) { return this.request<MemoryEntry>('memories/resolve', 'POST', body); }
  /** 遗忘绑定用户实际看到的版本，冲突后不自动重新提交。 */
  deleteMemory(id: number, expectedRevision: number) {
    return this.request<void>(`memories/${id}?expected_revision=${expectedRevision}`, 'DELETE');
  }
  /** 合并预览不会写入，离开页面时可以安全取消。 */
  previewMemoryMerge(body: MemoryMerge, signal?: AbortSignal) {
    return this.request<MemoryPreview>('memories/merge-preview', 'POST', body, signal);
  }
  /** 正式合并固定预览批次，不能重新抓取同主题新增数据。 */
  mergeMemories(body: MemoryMerge & {digest: string}) {
    return this.request<MemoryEntry>('memories/merge', 'POST', body);
  }
  /** 清理预览只检查指定主题与作用域。 */
  previewMemoryCleanup(body: {scope_id: string; topic: string; action: 'expire' | 'forget'}, signal?: AbortSignal) {
    return this.request<MemoryPreview>('memories/cleanup-preview', 'POST', body, signal);
  }
  /** 提交已确认的预览版本，摘要过期时由服务端明确拒绝。 */
  cleanupMemories(body: {items: MemoryVersion[]; action: 'expire' | 'forget'; digest: string; reason: string}) {
    return this.request<unknown>('memories/cleanup', 'POST', body);
  }
  /** 当前作用域的标签数用于筛选选项。 */
  memoryTags(filters: MemoryFilters = {}, signal?: AbortSignal) {
    const scope = {scope: filters.scope, scope_id: filters.scope_id};
    return this.request<TagCount[]>(`memory-tags?${queryString(scope)}`, 'GET', undefined, signal);
  }
  /** 匿名作用域可用于新增，不猜测或反查 QQ 身份。 */
  memoryScopes(signal?: AbortSignal) { return this.request<MemoryScopeInfo[]>('memory-scopes', 'GET', undefined, signal); }
}
