import { ApiClient } from './api';
import type { Page } from './ai-types';
import type { RunStep } from './agent-types';

export type CallType = 'ordinary' | 'agent' | 'auto_speak' | 'unknown';
export type CallCategory = 'conversation' | 'system';
export type SystemKind = 'auto_speak' | 'command';
export interface CallSummary {
  id: number; session_Id: string | null; create_time: string | null;
  duration: number | null; model: string | null; request_id: string | null;
  system_kind?: SystemKind | null;
  type: CallType; source: string; source_id: string; execution_id: string | null; status: string;
}
export interface CallDetail extends CallSummary {
  content: string | null; replay_context: string | null; user_certificate: string | null;
  data: unknown; request_context: unknown;
  tool_steps?: RunStep[];
}
export interface CallFilters { type: string; session_id: string; status: string; category?: CallCategory; system_kind?: string }

/** 统一调用记录沿用管理员认证，筛选条件与游标一起提交。 */
export class CallRecordsApi extends ApiClient {
  list(filters: CallFilters, cursor?: string, signal?: AbortSignal) {
    const query = new URLSearchParams({limit: '50'});
    Object.entries(filters).forEach(([key, value]) => { if (value) query.set(key, value); });
    if (cursor) query.set('cursor', cursor);
    return this.request<Page<CallSummary>>(`ai/records?${query}`, 'GET', undefined, signal);
  }
  detail(id: number, signal?: AbortSignal) {
    return this.request<CallDetail>(`ai/records/${id}`, 'GET', undefined, signal);
  }
}
