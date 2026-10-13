import type { BotInfo, ChatMessage, ChatResponse, JsonValue, ServiceStatus, SettingsSnapshot } from './types';
import { readChatEvents } from './stream';

export class ApiFailure extends Error {
  constructor(message: string, public status: number, public code: string, public requestId?: string) {
    super(message);
  }
}

export class ApiClient {
  constructor(protected token: string) {}

  protected async request<T>(path: string, method = 'GET', body?: unknown, signal?: AbortSignal): Promise<T> {
    // 免登录模式省略认证头，避免把空密码伪装成 Bearer 凭据。
    const response = await fetch(`/api/v1/${path}`, {
      method,
      headers: { ...(this.token ? { Authorization: `Bearer ${this.token}` } : {}), ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      signal,
    });
    if (response.status === 204) return undefined as T;
    const value = await response.json();
    if (!response.ok) throw new ApiFailure(value.message || '服务请求失败', response.status, value.code, value.request_id);
    return value as T;
  }

  /** 查询公开登录模式，不读取任何管理员密码。 */
  auth(signal?: AbortSignal) { return this.request<{ password_required: boolean }>('auth', 'GET', undefined, signal); }
  status() { return this.request<ServiceStatus>('status'); }
  bots() { return this.request<BotInfo[]>('bots'); }
  settings() { return this.request<SettingsSnapshot>('settings'); }
  updateSettings(expected_revision: number, values: Record<string, JsonValue>) {
    return this.request<SettingsSnapshot>('settings', 'PATCH', { expected_revision, values });
  }
  chat(prompt: string, session_id: string, messages: ChatMessage[], model?: string, signal?: AbortSignal) {
    return this.request<ChatResponse>('chat', 'POST', { prompt, session_id, messages, ...(model ? {model} : {}) }, signal);
  }
  async *streamChat(body: {prompt: string; session_id: string; messages: ChatMessage[]; model?: string}, signal: AbortSignal) {
    const response = await fetch('/api/v1/chat/stream', {method: 'POST', signal,
      headers: {Authorization: `Bearer ${this.token}`, 'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    if (!response.ok) { const value = await response.json(); throw new ApiFailure(value.message || '对话请求失败', response.status, value.code, value.request_id); }
    yield* readChatEvents(response, signal);
  }
}

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : '服务请求失败';
}
