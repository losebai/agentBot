import { ApiClient, ApiFailure } from './api';
import type { Page } from './ai-types';
import type { LibraryMaterial, MaterialLibraryFilters } from './material-library-types';

/** 管理员素材库单独使用跨来源只读 API，不复用临时附件或会话素材接口。 */
export class MaterialLibraryApi extends ApiClient {
  /** 游标随原筛选传递，筛选变化由页面清空游标。 */
  list(filters: MaterialLibraryFilters, cursor?: string, signal?: AbortSignal) {
    const query = new URLSearchParams({limit: '20'});
    for (const [key, value] of Object.entries(filters)) if (value.trim()) query.set(key, value.trim());
    if (cursor) query.set('cursor', cursor);
    return this.request<Page<LibraryMaterial>>(`material-library?${query}`, 'GET', undefined, signal);
  }

  /** 详情读取完整说明，原件仍需独立认证。 */
  detail(id: string, signal: AbortSignal) {
    return this.request<LibraryMaterial>(`material-library/${encodeURIComponent(id)}`, 'GET', undefined, signal);
  }

  /** 认证头只发给同源内容接口；迟到响应不再读取正文并关闭流。 */
  async content(id: string, signal: AbortSignal): Promise<Blob> {
    const response = await fetch(`/api/v1/material-library/${encodeURIComponent(id)}/content`, {
      headers: this.token ? {Authorization: `Bearer ${this.token}`} : {}, signal,
    });
    if (signal.aborted) {
      await response.body?.cancel();
      throw new DOMException('素材读取已取消', 'AbortError');
    }
    if (!response.ok) {
      const value = await response.json().catch(() => ({}));
      throw new ApiFailure(value.message || '素材读取失败', response.status, value.code || 'MATERIAL_FAILED', value.request_id);
    }
    return response.blob();
  }
}
