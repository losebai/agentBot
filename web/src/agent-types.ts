import type { TokenUsage } from './ai-types';
import type { ChatMessage, JsonValue } from './types';
import type { MediaAsset } from './media-types';

export interface DirectoryDraft { name: string; description: string; enabled: boolean }
export interface Versioned { id: number; revision: number; created_at?: string; updated_at?: string }
export interface AgentDraft extends DirectoryDraft { code: string; model_id: number; system_prompt: string; skill_ids: number[]; mcp_ids: number[]; tools: string[]; max_steps: number; options: Record<string, JsonValue> }
export interface Agent extends AgentDraft, Versioned {}
export interface SkillDraft extends DirectoryDraft { category: string; system_prompt: string; sample_prompt: string; tools: string[] }
export interface Skill extends SkillDraft, Versioned {}
export interface McpDraft extends DirectoryDraft { code: string; category: 'general'|'search'; transport: 'streamable'|'sse'; endpoint: string; credential_ref: string|null; allowed_tools: string[]; search_tool: string|null; timeout_seconds: number; required: boolean }
export interface McpServer extends McpDraft, Versioned { credential_available: boolean }
export interface AgentProbe { agent_code: string; prompt: string; messages: ChatMessage[]; session_id: string; attachment_ids?:string[]; request_id?: string; max_steps?: number; timeout_seconds?: number }
export interface AgentMetadata { execution_id: string; request_id: string; model: string; usage: TokenUsage; duration_ms: number; record_saved: boolean; backend: 'python-agent'; attachments?:MediaAsset[] }
export interface AgentCompletion extends AgentMetadata { content: string }
export interface AgentEvent { kind: 'start'|'model_start'|'text_delta'|'tool_start'|'tool_result'|'artifact'|'warning'|'done'|'error'; execution_id: string; sequence: number; turn: number; delta?: string; tool?: string; call_id?: string; code?: string; message?: string; metadata?: AgentMetadata; duration_ms?: number; attachment?:MediaAsset }
export interface RunSummary { execution_id: string; session_id: string; request_id: string; entry: 'admin'|'qq'; model: string; backend: 'python-agent'; status: 'running'|'completed'|'failed'|'cancelled'; duration_ms: number; usage: TokenUsage; created_at: string; error_code: string|null }
export interface RunDetail extends RunSummary { prompt: string; content: string; revisions: Record<string, number>; attachments?:MediaAsset[] }
export interface RunStep { execution_id: string; sequence: number; turn: number; kind: string; tool: string|null; call_id: string|null; status: 'started'|'completed'|'failed'|'unknown'; duration_ms: number|null; summary: string }
