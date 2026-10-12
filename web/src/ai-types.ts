export type ProviderProtocol = 'openai-responses' | 'openai-chat' | 'ollama';
export interface ProviderDraft { name: string; protocol: ProviderProtocol; base_url: string; credential_ref: string | null; enabled: boolean; description: string }
export interface Provider extends ProviderDraft { id: number; revision: number; credential_available: boolean }
export interface ModelDraft { alias: string; upstream_model: string; provider_id: number; enabled: boolean; media?:import('./media-types').ModelMediaCapabilities }
export interface AiModel extends ModelDraft { id: number; revision: number; media_initialized?:boolean }
export interface Page<T> { items: T[]; next_cursor: string | null }
export interface TokenUsage { prompt_tokens: number | null; completion_tokens: number | null; total_tokens: number | null }
export interface CompletionMetadata { execution_id: string; request_id: string; model: string; finish_reason: string; usage: TokenUsage; duration_ms: number; record_saved: boolean }
export interface AiEvent { kind: 'start' | 'delta' | 'done' | 'error'; request_id: string; delta?: string; metadata?: CompletionMetadata; code?: string; message?: string }
export interface SessionSummary { execution_id: string; session_id: string; request_id: string; configured_model: string; model: string; status: 'completed' | 'failed' | 'cancelled'; duration_ms: number; usage: TokenUsage; created_at: string; error_code: string | null }
export interface SessionDetail extends SessionSummary { prompt: string; content: string }
