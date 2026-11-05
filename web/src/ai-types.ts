export type ProviderProtocol = 'openai-responses' | 'openai-chat' | 'ollama';
export interface ProviderDraft { name: string; protocol: ProviderProtocol; base_url: string; user_agent: string; credential?: string | null; enabled: boolean; description: string }
export interface Provider extends Omit<ProviderDraft, 'credential'> { id: number; revision: number; credential_set: boolean; credential_available: boolean }
export interface ProviderCredential { credential: string }
export interface UpstreamModels { provider_id: number; revision: number; models: string[] }
export interface ModelImportItem { upstream_model: string }
export interface ModelImportResult { provider_id: number; revision: number; imported: AiModel[]; skipped: string[]; conflicts: ModelImportItem[] }
export interface ModelDraft { upstream_model: string; provider_id: number; enabled: boolean; media?:import('./media-types').ModelMediaCapabilities }
/** 历史别名仅用于读取旧配置；新绑定始终提交稳定 reference。 */
export interface AiModel extends ModelDraft { id: number; revision: number; provider_name: string; reference: string; alias?: string | null; media_initialized?:boolean }
export interface Page<T> { items: T[]; next_cursor: string | null }
export interface CatalogPage<T> extends Page<T> { total: number }
/** 缓存输入属于输入 Token 的子集，未上报时不能视为零命中。 */
export interface TokenUsage { prompt_tokens: number | null; completion_tokens: number | null; total_tokens: number | null; cached_tokens?: number | null; cache_prompt_tokens?: number | null }
export interface CompletionMetadata { execution_id: string; request_id: string; model: string; finish_reason: string; usage: TokenUsage; duration_ms: number; record_saved: boolean }
export interface AiEvent { kind: 'start' | 'delta' | 'done' | 'error'; request_id: string; delta?: string; metadata?: CompletionMetadata; code?: string; message?: string }
export interface SessionSummary { execution_id: string; session_id: string; request_id: string; configured_model: string; model: string; status: 'completed' | 'failed' | 'cancelled'; duration_ms: number; usage: TokenUsage; created_at: string; error_code: string | null }
export interface SessionDetail extends SessionSummary { prompt: string; content: string }
