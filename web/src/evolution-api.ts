import { ApiClient } from './api';

export type Traits = Record<'verbosity' | 'warmth' | 'humor' | 'initiative', number>;
export interface EvolutionConfig { enabled: boolean; revision: number }
export interface EvolutionProfile {
  id: number; agent_id: number | null; scope_key: string; subject_key: string; traits: Traits; revision: number;
}
export type EvolutionKind = 'feedback' | 'change' | 'reset';
export interface EvolutionEvent {
  id: number; kind: EvolutionKind; dimension: string;
  direction: number; excerpt: string; observed_at: string;
  before_value: number | null; after_value: number | null; evidence_ids: number[];
}
export interface EvolutionPage {
  config: EvolutionConfig; items: EvolutionProfile[]; next_cursor: number | null; total: number;
}
export interface EvolutionHistory {
  profile: EvolutionProfile; items: EvolutionEvent[]; next_cursor: number | null; total: number;
}
export interface EvolutionRecord extends EvolutionEvent {
  profile_id: number; subject_key: string; scope_key: string;
  agent_id: number | null; agent_name: string | null;
}
export interface EvolutionRecordsPage {
  items: EvolutionRecord[]; next_cursor: number | null; total: number;
}

export interface EvolutionResearch {
  profile_id: number; revision: number; preview_key: string; before: Traits; after: Traits;
  changes: { dimension: keyof Traits; before: number; after: number; net_support: number; dates: number; eligible: boolean; evidence_ids: number[] }[];
}

/** 管理控制只能研究既有证据并按版本应用，不能伪造原话、身份或任意档位。 */
export class EvolutionApi extends ApiClient {
  /** 不指定角色时读取通用习惯；角色范围仍支持取消及主键分页。 */
  profiles(agent: number | null, signal: AbortSignal, page = 1, limit = 10) {
    const query = new URLSearchParams({ page: String(page), limit: String(limit) });
    if (agent !== null) query.set('agent_id', String(agent));
    return this.request<EvolutionPage>(`evolution?${query}`, 'GET', undefined, signal);
  }
  /** 画像身份直接定位任何范围的依据，不由当前角色选择限制审计详情。 */
  history(profile: number, signal: AbortSignal, page = 1, limit = 10) {
    return this.request<EvolutionHistory>(`evolution/profiles/${profile}?page=${page}&limit=${limit}`, 'GET', undefined, signal);
  }
  /** 保存时绑定已读取版本，失败由调用者保留原开关。 */
  configure(agent: number | null, config: EvolutionConfig) {
    const query = agent === null ? '' : `?agent_id=${agent}`;
    return this.request<EvolutionConfig>(`evolution${query}`, 'PATCH', {
      enabled: !config.enabled, expected_revision: config.revision,
    });
  }
  /** 重置消费旧证据，后端保留变化记录。 */
  reset(profile: EvolutionProfile) {
    return this.request<EvolutionProfile>(`evolution/profiles/${profile.id}/reset`, 'POST', {
      expected_revision: profile.revision,
    });
  }

  /** 预览不消费证据；手动应用绑定该次研究摘要。 */
  preview(id: number, signal?: AbortSignal) { return this.request<EvolutionResearch>(`evolution/profiles/${id}/preview`, 'GET', undefined, signal); }
  advance(research: EvolutionResearch) { return this.request<EvolutionProfile>(`evolution/profiles/${research.profile_id}/advance`, 'POST', { expected_revision: research.revision, preview_key: research.preview_key }); }

  /** 列表默认跨所有范围；类型与关联条件由用户显式筛选，游标始终倒序。 */
  records(signal: AbortSignal, page = 1, limit = 10, filters: {
    kind?: EvolutionKind; agentId?: number; unbound?: boolean;
  } = {}) {
    const query = new URLSearchParams({ page: String(page), limit: String(limit) });
    if (filters.kind) query.set('kind', filters.kind);
    if (filters.agentId !== undefined) query.set('agent_id', String(filters.agentId));
    if (filters.unbound) query.set('unbound', 'true');
    return this.request<EvolutionRecordsPage>(`evolution/records?${query}`, 'GET', undefined, signal);
  }
}
