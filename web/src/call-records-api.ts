import { ApiClient } from './api';
import type { Page, TokenUsage } from './ai-types';
import type { RunStep } from './agent-types';

export type CallType = 'ordinary' | 'agent' | 'auto_speak' | 'unknown';
export type CallCategory = 'conversation' | 'system';
export type SystemKind = 'auto_speak' | 'command';
export interface CallSummary {
  id: number; session_Id: string | null; create_time: string | null;
  duration: number | null; model: string | null; request_id: string | null;
  system_kind?: SystemKind | null;
  type: CallType; source: string; source_id: string; execution_id: string | null; status: string;
  /** 单次归档的累计用量；未上报字段保留 null，缓存输入已包含在输入总量中。 */
  usage: TokenUsage;
}
export interface CallDetail extends CallSummary {
  content: string | null; replay_context: string | null; user_certificate: string | null;
  data: unknown; request_context: unknown;
  tool_steps?: RunStep[];
}
export interface CallFilters { type: string; session_id: string; status: string; category?: CallCategory; system_kind?: string }

/** 统一调用记录沿用管理员认证，列表和独立计数使用同一组筛选条件。 */
export class CallRecordsApi extends ApiClient {
  /** 页码直接传给后端，不能先拉完前面的所有页再在浏览器切片。 */
  list(filters: CallFilters, page = 1, pageSize = 20, signal?: AbortSignal) {
    const query = this.filters(filters);
    query.set('limit', String(pageSize)); query.set('page', String(page));
    return this.request<Page<CallSummary>>(`ai/records?${query}`, 'GET', undefined, signal);
  }
  /** 总数独立于列表读取，翻页只复用同一筛选的总数，不重复扫描全部归档。 */
  count(filters: CallFilters, signal?: AbortSignal) {
    return this.request<{ total: number }>(`ai/records/count?${this.filters(filters)}`, 'GET', undefined, signal);
  }
  /** 列表与计数共同忽略空筛选值，避免两次请求条件不一致。 */
  private filters(filters: CallFilters) {
    const query = new URLSearchParams();
    Object.entries(filters).forEach(([key, value]) => { if (value) query.set(key, value); });
    return query;
  }
  detail(id: number, signal?: AbortSignal) {
    return this.request<CallDetail>(`ai/records/${id}`, 'GET', undefined, signal);
  }
}
