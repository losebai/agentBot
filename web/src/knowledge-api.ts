import { ApiClient } from './api';
import type { Page } from './ai-types';
import type { KnowledgeDraft, KnowledgeEntry, KnowledgeFilters, MemoryDraft, MemoryEntry, MemoryFilters, MemoryScopeInfo, TagCount } from './knowledge-types';

/** 省略空筛选，分页游标始终携带当前提交的条件。 */
function queryString(filters: MemoryFilters = {}, cursor?: string) {
  const params = new URLSearchParams({limit: '20'});
  for (const [key, value] of Object.entries(filters)) if (value) params.set(key, value);
  if (cursor) params.set('cursor', cursor);
  return params.toString();
}

/** 知识与记忆只提供新增、读取和删除，无更新方法。 */
export class KnowledgeApi extends ApiClient {
  /** 列表查询允许页面离开或变更筛选时取消。 */
  listKnowledge(filters: KnowledgeFilters = {}, cursor?: string, signal?: AbortSignal) {
    return this.request<Page<KnowledgeEntry>>(`knowledge?${queryString(filters, cursor)}`, 'GET', undefined, signal);
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
  listMemories(filters: MemoryFilters = {}, cursor?: string, signal?: AbortSignal) {
    return this.request<Page<MemoryEntry>>(`memories?${queryString(filters, cursor)}`, 'GET', undefined, signal);
  }
  /** 读取完整事实，页面详情为只读。 */
  getMemory(id: number, signal?: AbortSignal) { return this.request<MemoryEntry>(`memories/${id}`, 'GET', undefined, signal); }
  /** 已有键返回冲突，客户端不改用更新或恢复。 */
  createMemory(body: MemoryDraft) { return this.request<MemoryEntry>('memories', 'POST', body); }
  /** 逻辑删除不能被后续同键保存恢复。 */
  deleteMemory(id: number) { return this.request<void>(`memories/${id}`, 'DELETE'); }
  /** 当前作用域的标签数用于筛选选项。 */
  memoryTags(filters: MemoryFilters = {}, signal?: AbortSignal) {
    const scope = {scope: filters.scope, scope_id: filters.scope_id};
    return this.request<TagCount[]>(`memory-tags?${queryString(scope)}`, 'GET', undefined, signal);
  }
  /** 匿名作用域可用于新增，不猜测或反查 QQ 身份。 */
  memoryScopes(signal?: AbortSignal) { return this.request<MemoryScopeInfo[]>('memory-scopes', 'GET', undefined, signal); }
}
