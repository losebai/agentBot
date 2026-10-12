import type { AiEvent } from './ai-types';

export async function* readChatEvents(response: Response, signal: AbortSignal): AsyncGenerator<AiEvent> {
  if (!response.body) throw new Error('服务未返回对话流');
  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8', { fatal: true });
  let pending = '', data: string[] = [], bytes = 0, terminal = false, replyBytes = 0;
  const abort = () => { void reader.cancel(); };
  signal.addEventListener('abort', abort, { once: true });
  const event = (): AiEvent | null => {
    if (!data.length) return null;
    const value: unknown = JSON.parse(data.join('\n'));
    data = []; bytes = 0;
    if (!value || typeof value !== 'object' || !('kind' in value) || !('request_id' in value) ||
        !['start', 'delta', 'done', 'error'].includes(String(value.kind)) || typeof value.request_id !== 'string') throw new Error('对话流格式无效');
    const item = value as AiEvent;
    if (item.kind === 'delta') {
      if (typeof item.delta !== 'string') throw new Error('对话流格式无效');
      replyBytes += new TextEncoder().encode(item.delta).byteLength;
      if (replyBytes > 1048576) throw new Error('回复超过大小限制');
    }
    if (item.kind === 'done' && (!item.metadata || typeof item.metadata.record_saved !== 'boolean')) throw new Error('完成事件缺少结果信息');
    return item;
  };
  try {
    while (true) {
      if (signal.aborted) throw new DOMException('已停止', 'AbortError');
      const chunk = await reader.read();
      if (signal.aborted) throw new DOMException('已停止', 'AbortError');
      pending += decoder.decode(chunk.value, { stream: !chunk.done });
      while (pending.includes('\n')) {
        const index = pending.indexOf('\n');
        const line = pending.slice(0, index).replace(/\r$/, '');
        pending = pending.slice(index + 1);
        bytes += new TextEncoder().encode(line).byteLength + 1;
        if (bytes > 262144) throw new Error('对话事件超过大小限制');
        if (!line) {
          const item = event(); bytes = 0;
          if (item) {
            terminal = item.kind === 'done' || item.kind === 'error';
            yield item;
            if (terminal) return;
          }
        } else if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''));
      }
      if (new TextEncoder().encode(pending).byteLength + bytes > 262144) throw new Error('对话事件超过大小限制');
      if (chunk.done) break;
    }
    if (!terminal) throw new Error('对话流未完成，请重试或查看会话记录');
  } finally {
    signal.removeEventListener('abort', abort);
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}
