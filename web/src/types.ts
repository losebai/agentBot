export type JsonValue = string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue };

export interface SettingField {
  name: string;
  description: string;
  kind: 'boolean' | 'number' | 'string' | 'json' | 'secret';
  value: JsonValue;
  nullable: boolean;
  secret_is_set: boolean;
  restart_required: boolean;
}

export interface SettingsSnapshot {
  database_revision: number;
  applied_revision: number;
  restart_required_fields: string[];
  fields: SettingField[];
}

export interface ServiceStatus {
  service: 'agentBot';
  ready: boolean;
  bot_count: number;
  database_revision: number;
  applied_revision: number;
  restart_required_fields: string[];
  ai_backend: 'java-compatibility' | 'python-ordinary';
  plain_ai_backend?: 'java' | 'python';
  configured_model?: string;
  agent_backend?: 'java' | 'python';
  configured_agent?: string | null;
  python_agent?: { initialized: boolean; agent_available: boolean; model_available: boolean; code: string | null } | null;
  legacy_capabilities?: string[];
  media_backend?: 'java' | 'python';
  python_media?: { initialized:boolean; storage_available:boolean; code:string|null } | null;
  python_ai?: { initialized: boolean; model_available: boolean; provider_available: boolean; credential_available: boolean; code: string | null } | null;
}

export interface BotInfo { id: string; adapter: string }

/** Preserves the role/content wire shape from bai-web/src/services/ai/types.ts. */
export interface ChatMessage { role: 'system' | 'user' | 'assistant'; content: string }
export interface ChatResponse { content: string; request_id: string; metadata?: import('./ai-types').CompletionMetadata | null }
