import type { BotInfo, ChatMessage, ChatResponse, DeadLetterClearResult, DeadLetterStatus, JsonValue, NapCatStatus, PluginInfo, ServiceStatus, SettingsSnapshot } from './types';
import { readChatEvents } from './stream';
import type { BilibiliQr, BilibiliStatus } from './components/BilibiliPlugin';

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
  /** 概览按北京时间日期区间读取，取消旧请求以阻止过期统计覆盖新筛选。 */
  status(signal?: AbortSignal, range?: { start_date: string; end_date: string }) {
    const query = range ? `?${new URLSearchParams(range)}` : '';
    return this.request<ServiceStatus>(`status${query}`, 'GET', undefined, signal);
  }
  /** 真实 QQ 连接来自适配器，不能用 NapCat 进程状态替代。 */
  bots(signal?: AbortSignal) { return this.request<BotInfo[]>('bots', 'GET', undefined, signal); }
  /** 只读观察所属托管实例，不触发程序启动或重启。 */
  napcatStatus(signal?: AbortSignal) { return this.request<NapCatStatus>('napcat/status', 'GET', undefined, signal); }
  /** QQ 插件只读取所属桌面能力，不接收可执行文件、端口或登录密钥。 */
  napcatPlugin(signal?: AbortSignal) {
    return this.request<{ available: boolean; plugin_enabled: boolean; status: NapCatStatus }>('napcat/plugin', 'GET', undefined, signal);
  }
  /** 用户显式启动所属 NapCat，不修改静态配置或重启后端。 */
  startNapcat() { return this.request<NapCatStatus>('napcat/start', 'POST', {}); }
  /** 仅发送打开原生登录窗口的意图，WebUI 密钥不经过页面。 */
  openNapcatLogin() { return this.request<{ accepted: boolean }>('napcat/login', 'POST', {}); }
  /** 页面卸载时取消读取，避免过期响应覆盖插件草稿。 */
  settings(signal?: AbortSignal) { return this.request<SettingsSnapshot>('settings', 'GET', undefined, signal); }
  /** 只读查询运行中的 QQ 队列；离开维护区时取消等待。 */
  deadLetters(signal?: AbortSignal) { return this.request<DeadLetterStatus>('queues/dead-letters', 'GET', undefined, signal); }
  /** 仅清除死信记录，复用管理认证与错误处理，不提交任何配置。 */
  clearDeadLetters(signal?: AbortSignal) { return this.request<DeadLetterClearResult>('queues/dead-letters', 'DELETE', undefined, signal); }
  /** 插件目录返回已保存选择及当前进程状态，不触发平台连接。 */
  plugins(signal?: AbortSignal) { return this.request<PluginInfo[]>('plugins', 'GET', undefined, signal); }
  /** 每次实际检查当前 B 站账号，不返回凭据。 */
  bilibiliStatus(signal?: AbortSignal) { return this.request<BilibiliStatus>('bilibili/status', 'GET', undefined, signal); }
  /** 新二维码替换旧扫码会话。 */
  bilibiliGenerateQr(signal?: AbortSignal) { return this.request<BilibiliQr>('bilibili/login/qr', 'POST', {}, signal); }
  /** 轮询本地会话 ID，平台扫码 key 仅留在后端。 */
  bilibiliPollQr(id: string, signal?: AbortSignal) { return this.request<Pick<BilibiliQr, 'status'>>(`bilibili/login/qr/${encodeURIComponent(id)}`, 'GET', undefined, signal); }
  /** 清除本地授权，不声称其他设备退出。 */
  bilibiliClearLogin() { return this.request<{ cleared: boolean }>('bilibili/login', 'DELETE'); }
  /** 运行草稿使用数据库版本；包含静态修改时另传启动文件版本防止覆盖外部编辑。 */
  updateSettings(expected_revision: number, values: Record<string, JsonValue>, startup_revision?: string | null) {
    return this.request<SettingsSnapshot>('settings', 'PATCH', { expected_revision, values,
      ...(startup_revision === undefined ? {} : { startup_revision }) });
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
