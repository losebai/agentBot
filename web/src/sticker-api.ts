import { ApiClient, ApiFailure } from './api';
import type { StickerMatch, StickerPage, StickerPatch, StickerView } from './sticker-types';

/** 表情包管理仅复用同源管理员认证，URL 不携带令牌和本地文件路径。 */
export class StickerApi extends ApiClient {
  /** 名称及标签筛选使用偏移分页，切换筛选时由页面归零。 */
  list(query: string, tag: string, offset: number, signal: AbortSignal) {
    const params = new URLSearchParams({ query, tag, offset: String(offset), limit: '20' });
    return this.request<StickerPage>(`stickers?${params}`, 'GET', undefined, signal);
  }

  /** 每张原件独立提交，自动识图由本批开关决定，不能因配置过模型强制调用。 */
  async upload(file: File, name: string, tags: string[], signal: AbortSignal, autoAnalyze = true): Promise<StickerView> {
    const params = new URLSearchParams({ name, tags: JSON.stringify(tags), auto_analyze: String(autoAnalyze) });
    const response = await fetch(`/api/v1/stickers/uploads?${params}`, {
      method: 'POST', headers: { ...(this.token ? { Authorization: `Bearer ${this.token}` } : {}),
        'Content-Type': file.type || 'application/octet-stream' }, body: file, signal,
    });
    await check(response);
    return response.json();
  }

  /** 图片需通过认证读取 Blob，静态预览与原件使用独立生命周期。 */
  async content(id: string, preview: boolean, signal: AbortSignal): Promise<Blob> {
    const response = await fetch(`/api/v1/stickers/${encodeURIComponent(id)}/content?preview=${preview}`, {
      headers: this.token ? { Authorization: `Bearer ${this.token}` } : {}, signal,
    });
    if (signal.aborted) {
      await response.body?.cancel();
      throw new DOMException('图片读取已取消', 'AbortError');
    }
    await check(response);
    return response.blob();
  }

  /** 修订冲突直接展示，失败保留草稿且不自动重试覆盖。 */
  update(id: string, patch: StickerPatch, signal: AbortSignal) {
    return this.request<StickerView>(`stickers/${encodeURIComponent(id)}`, 'PATCH', patch, signal);
  }

  /** 显式重识图保留管理员标签，使用当前修订避免覆盖外部编辑。 */
  analyze(id: string, expected_revision: number, signal: AbortSignal) {
    return this.request<StickerView>(`stickers/${encodeURIComponent(id)}/analyze`, 'POST', { expected_revision }, signal);
  }

  /** 删除仍要求修订一致，管理员确认后仅提交对应库条目。 */
  remove(id: string, expected_revision: number, signal: AbortSignal) {
    return this.request<{deleted: boolean}>(`stickers/${encodeURIComponent(id)}?expected_revision=${expected_revision}`, 'DELETE', undefined, signal);
  }

  /** 试用只检索不发送，使用与机器人相同的语境匹配接口。 */
  search(context: string, tags: string[], signal: AbortSignal) {
    return this.request<{matches: StickerMatch[]}>('stickers/search', 'POST', { context, tags, limit: 5 }, signal);
  }
}

/** 原始图片请求沿用管理 API 错误格式，代理非 JSON 错误保留清晰提示。 */
async function check(response: Response) {
  if (response.ok) return;
  const body = await response.json().catch(() => ({}));
  throw new ApiFailure(body.message || '表情包请求失败', response.status, body.code || 'STICKER_FAILED', body.request_id);
}
