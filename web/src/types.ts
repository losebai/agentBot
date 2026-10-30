export type JsonValue = string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue };

/** 保存启用状态与进程运行状态分开，修改开关需重启后生效。 */
export interface PluginInfo {
  id: string;
  name: string;
  version: string;
  description: string;
  saved_enabled: boolean;
  enabled: boolean;
  status: 'disabled' | 'prepared' | 'running' | 'stopped' | 'failed';
  restart_required: boolean;
}

export interface SettingField {
  name: string;
  description: string;
  kind: 'boolean' | 'number' | 'string' | 'json' | 'secret';
  value: JsonValue;
  nullable: boolean;
  secret_is_set: boolean;
  restart_required: boolean;
  /** 静态字段由启动文件提供，只有服务端明确允许的字段可保存；环境覆盖时只读。 */
  read_only?: boolean;
  scope?: 'static' | 'runtime';
}

export interface SettingsSnapshot {
  database_revision: number;
  applied_revision: number;
  restart_required_fields: string[];
  /** 启动文件并发校验版本，实际修改任何静态字段时提交，不能取代数据库版本。 */
  startup_revision: string | null;
  fields: SettingField[];
}

/** 队列未运行时仅展示不可用状态，不将零计数误认为队列已清空。 */
export interface DeadLetterStatus {
  available: boolean;
  text: number;
  images: number;
  total: number;
}

/** 清除结果按本次实际删除数量返回，不包含待处理任务。 */
export interface DeadLetterClearResult {
  cleared: number;
  text: number;
  images: number;
}

/** 日期区间内的归档与实时请求分开统计，无有效分母的比例保持未知。 */
export interface OverviewStatistics {
  conversation_count: number;
  running_requests: number;
  total_tokens: number;
  prompt_tokens: number;
  completion_tokens: number;
  cached_tokens: number;
  cache_prompt_tokens: number;
  cache_hit_rate: number | null;
  model_call_count: number;
  failed_model_calls: number;
  model_failure_rate: number | null;
}

/** 当前有效记忆总量；召回按所选日期计数，没有已完成召回时比例未知。 */
export interface MemoryStatistics {
  memory_count: number;
  recall_count: number;
  recall_hit_count: number;
  recall_rate: number | null;
}

export interface ServiceStatus {
  service: 'agentBot';
  ready: boolean;
  bot_count: number;
  database_revision: number;
  applied_revision: number;
  statistics?: OverviewStatistics | null;
  statistics_error?: string | null;
  memory_statistics?: MemoryStatistics | null;
  memory_statistics_error?: string | null;
  restart_required_fields: string[];
  ai_backend: 'python-ordinary';
  plain_ai_backend?: 'python';
  configured_model?: string;
  agent_backend?: 'python';
  configured_agent?: string | null;
  python_agent?: { initialized: boolean; agent_available: boolean; model_available: boolean; code: string | null } | null;
  legacy_capabilities?: string[];
  job_enabled?: boolean;
  auto_speak_enabled?: boolean;
  media_backend?: 'python';
  python_media?: { initialized:boolean; storage_available:boolean; code:string|null } | null;
  python_ai?: { initialized: boolean; model_available: boolean; provider_available: boolean; credential_available: boolean; code: string | null } | null;
}

export interface BotInfo { id: string; adapter: string }

/** Preserves the role/content wire shape from bai-web/src/services/ai/types.ts. */
export interface ChatMessage { role: 'system' | 'user' | 'assistant'; content: string }
export interface ChatResponse { content: string; request_id: string; metadata?: import('./ai-types').CompletionMetadata | null }
/** 托管状态不包含密钥、程序命令或本机路径。 */
export interface NapCatStatus {
  mode: 'external' | 'managed';
  state: 'not_started' | 'starting' | 'running' | 'stopped' | 'failed' | 'plugin_disabled';
  webui_ready: boolean;
  error_code: string | null;
}
