import type { EvolutionResearch } from './evolution-api';
import { ApiClient } from './api';

export type Action = 'plan' | 'recall' | 'draft' | 'consolidate' | 'review' | 'report';
export interface PersistentIdentity { name: string; role: string; style: string; relationships: string; boundaries: string }
export interface AutonomyPolicy {
  paused: boolean; model: string; allowed_actions: Action[]; discover_tasks: boolean;
  auto_consolidate: boolean; auto_correct: boolean; notify_qq: boolean;
  max_model_calls_per_day: number; daily_cost_limit: number; input_cost_per_million: number | null;
  output_cost_per_million: number | null; max_attempts: number; min_report_interval_seconds: number;
  max_reports_per_day: number; quiet_start_hour: number; quiet_end_hour: number; episode_retention_days: number;
}
export interface LocalOwner { channel: 'local'; agent_key: string; participant_id: string; session_id: string }
export interface LegacyOwner { channel?: 'qq'; bot_id: string; session_id: string; sender_id: string; scene: 'private' | 'group'; group_id: string | null }
export type AutonomyOwner = LocalOwner | LegacyOwner;
/** 按渠道展示参与者，独立身份不需要平台号码。 */
export function participant(owner: AutonomyOwner) { return owner.channel === 'local' ? owner.participant_id : owner.sender_id; }
export interface Profile {
  id: string; revision: number; identity: PersistentIdentity; policy: AutonomyPolicy;
  owner: AutonomyOwner;
  state: { current_goal: string; current_task: string | null; activity: string; emotion: string; recent_errors: string[] };
  usage: { model_calls?: number; reserved_cost?: number; reports?: number };
}
export interface AutonomousTask {
  id: string; revision: number; goal: string; status: string; cursor: number; attempts: number;
  steps: { action: Action; text: string; tags: string[] }[]; outputs: unknown[]; error_code: string | null;
}
export interface MemoryProposal {
  id: string; revision: number; status: string; episode_id: string;
  document: { fact: { content: string; excerpt: string; kind: string; correction_id?: number | null;
    expected_revision?: number | null; duplicate_id?: number | null }; result: unknown; approved: boolean };
}
export interface AuditEntry { id: string; kind: string; created_at: string; subject_id: string | null; document_json: unknown }
export interface ConversationEntry {
  id: string; created_at: string; source_expired: boolean; task: AutonomousTask | null;
  episode: { id: string; text: string; source: 'self_report' | 'third_party'; occurred_at: string; summary: string | null } | null;
}
export interface ConversationPage { items: ConversationEntry[]; next_before: string | null }

/** 持续身份接口只提交有界数据和审核意图，页面不能调用系统命令或控制进程。 */
export class AutonomyApi extends ApiClient {
  /** 单页使用稳定主键游标，调用者显式加载下一页。 */
  profiles(after = '', signal?: AbortSignal) { return this.request<Profile[]>(`autonomy/profiles?limit=50&after=${after}`, 'GET', undefined, signal); }
  /** 只创建明确绑定，默认保持宿主暂停。 */
  create(body: { owner: Profile['owner']; identity: PersistentIdentity }) { return this.request<Profile>('autonomy/profiles', 'POST', body); }
  /** 按所见版本保存身份、策略或状态，绝不自行重试陈旧覆盖。 */
  update(profile: Profile, changes: Partial<Pick<Profile, 'identity' | 'policy' | 'state'>>) {
    return this.request<Profile>(`autonomy/profiles/${profile.id}`, 'PATCH', { expected_revision: profile.revision, ...changes });
  }
  /** 紧急暂停无需读取版本；全局入口暂停当前命名空间全部身份。 */
  pause(id?: string) { return this.request<void>(id ? `autonomy/profiles/${id}/pause` : 'autonomy/pause', 'POST'); }
  /** 查询步骤状态，返回正文仅由 React 文本节点渲染。 */
  tasks(id: string, after = '', signal?: AbortSignal) { return this.request<AutonomousTask[]>(`autonomy/profiles/${id}/tasks?limit=50&after=${after}`, 'GET', undefined, signal); }
  /** 创建持久目标，不在网页中启动执行器。 */
  createTask(id: string, goal: string, run_at: string | null) { return this.request<AutonomousTask>(`autonomy/profiles/${id}/tasks`, 'POST', { request_id: crypto.randomUUID(), goal, run_at }); }
  /** 计划审核绑定当前版本，取消后迟到执行结果会被数据库拒绝。 */
  decideTask(id: string, task: AutonomousTask, approve: boolean) { return this.request(`autonomy/profiles/${id}/tasks/${task.id}/decision`, 'POST', { expected_revision: task.revision, approve }); }
  /** 原始事件由管理员确认来源，模型摘要不能通过此接口伪造事件。 */
  ingest(id: string, text: string, task_requested: boolean, source: string) { return this.request(`autonomy/profiles/${id}/events`, 'POST', { event_id: crypto.randomUUID(), occurred_at: new Date().toISOString(), text, task_requested, source }); }
  /** 返回待审和已处理结论，事实正文与证据分开。 */
  proposals(id: string, after = '', signal?: AbortSignal) { return this.request<MemoryProposal[]>(`autonomy/profiles/${id}/proposals?limit=50&after=${after}`, 'GET', undefined, signal); }
  /** 提案审批不扩大事实作用域，也不跳过原事实版本校验。 */
  decideProposal(id: string, item: MemoryProposal, approve: boolean) { return this.request(`autonomy/profiles/${id}/proposals/${item.id}/decision`, 'POST', { expected_revision: item.revision, approve }); }
  /** 分层检索会核对授权和有效期，摘要保持独立标识。 */
  memories(id: string, query: string, signal?: AbortSignal) { return this.request<Record<string, unknown[]>>(`autonomy/profiles/${id}/memories?query=${encodeURIComponent(query)}`, 'GET', undefined, signal); }
  /** 审计不可在页面删除，分页游标由服务端提供。 */
  audit(id: string, before = '', signal?: AbortSignal) { return this.request<{ items: AuditEntry[]; next_before: string | null }>(`autonomy/profiles/${id}/audit?limit=50${before ? `&before=${before}` : ''}`, 'GET', undefined, signal); }
  /** 从创建审计恢复对话目录，当前步骤和投递状态始终来自持久记录。 */
  conversations(id: string, before = '', signal?: AbortSignal) { return this.request<ConversationPage>(`autonomy/profiles/${id}/conversations?limit=20${before ? `&before=${encodeURIComponent(before)}` : ''}`, 'GET', undefined, signal); }
  /** 独立网页消息只入队，后台执行器继续处理并持久化回复。 */
  send(id: string, text: string, requestId: string) { return this.request<AutonomousTask>(`autonomy/profiles/${id}/messages`, 'POST', { request_id: requestId, text }); }
  /** 独立身份自动定位自身演进画像，页面不能指定其他参与者的画像。 */
  evolution(id: string, signal?: AbortSignal) { return this.request<{ research: EvolutionResearch | null }>(`autonomy/profiles/${id}/evolution`, 'GET', undefined, signal); }
  advanceEvolution(id: string, value: EvolutionResearch) { return this.request(`autonomy/profiles/${id}/evolution`, 'POST', { expected_revision: value.revision, preview_key: value.preview_key }); }
  /** 固定机制验收只使用临时库，不调用付费模型。 */
  evaluate(id: string) { return this.request<Record<string, unknown>>(`autonomy/profiles/${id}/evaluate`, 'POST'); }
}
