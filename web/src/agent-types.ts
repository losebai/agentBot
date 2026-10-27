import type { TokenUsage } from './ai-types';
import type { ChatMessage, JsonValue } from './types';
import type { MediaAsset } from './media-types';

export interface DirectoryDraft { name: string; description: string; enabled: boolean }
export interface Versioned { id: number; revision: number; created_at?: string; updated_at?: string }
/** 聊天模型沿用原绑定，图片和错误转发空值分别表示沿用及关闭。 */
/** Agent 预设只管理能力与模型，工具调用预算由机器人配置统一提供。 */
export interface AgentDraft extends DirectoryDraft { code: string; model_id: number; vision_model_id?: number | null; fallback_model_id?: number | null; system_prompt: string; skill_ids: number[]; mcp_ids: number[]; tools: string[]; options: Record<string, JsonValue> }
export interface Agent extends AgentDraft, Versioned {}
export interface SkillDraft extends DirectoryDraft { category: string; system_prompt: string; sample_prompt: string; tools: string[] }
export interface Skill extends SkillDraft, Versioned {}
export interface McpDraft extends DirectoryDraft { code: string; category: 'general'|'search'; transport: 'streamable'|'sse'; endpoint: string; credential?: string|null; allowed_tools: string[]; search_tool: string|null; timeout_seconds: number; required: boolean }
export interface McpServer extends Omit<McpDraft, 'credential'>, Versioned { credential_set: boolean; credential_available: boolean }
/** 只读发现返回工具定义，开关状态来自服务的 allowed_tools 集合。 */
export interface McpTool { name: string; description: string; input_schema: Record<string, JsonValue>; source: 'mcp' }
export interface AgentProbe { agent_code: string; prompt: string; messages: ChatMessage[]; session_id: string; attachment_ids?:string[]; video_asset_id?:string; request_id?: string; max_steps?: number; timeout_seconds?: number }
export interface AgentMetadata { execution_id: string; request_id: string; model: string; usage: TokenUsage; duration_ms: number; record_saved: boolean; backend: 'python-agent'; attachments?:MediaAsset[] }
export interface AgentCompletion extends AgentMetadata { content: string }
export interface AgentEvent { kind: 'start'|'model_start'|'text_delta'|'tool_start'|'tool_result'|'artifact'|'warning'|'done'|'error'; execution_id: string; sequence: number; turn: number; delta?: string; tool?: string; call_id?: string; code?: string; message?: string; metadata?: AgentMetadata; duration_ms?: number; attachment?:MediaAsset }
export interface RunSummary { execution_id: string; session_id: string; request_id: string; entry: 'admin'|'qq'; model: string; backend: 'python-agent'; status: 'running'|'completed'|'failed'|'cancelled'; duration_ms: number; usage: TokenUsage; created_at: string; error_code: string|null }
export interface RunDetail extends RunSummary { prompt: string; content: string; revisions: Record<string, number>; attachments?:MediaAsset[] }
export interface RunStep { execution_id: string; sequence: number; turn: number; kind: string; tool: string|null; call_id: string|null; status: 'started'|'completed'|'failed'|'unknown'; duration_ms: number|null; summary: string; arguments?:Record<string, JsonValue>|null; result?:Record<string, JsonValue>|null }
