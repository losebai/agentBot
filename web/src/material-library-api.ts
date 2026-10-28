import { ApiClient, ApiFailure } from './api';
import type { Page } from './ai-types';
import type { LibraryMaterial, MaterialLibraryFilters } from './material-library-types';

/** 列表与统计使用同一组已去除空白的筛选，总数不包含分页参数。 */
function libraryQuery(filters: MaterialLibraryFilters) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) if (value.trim()) query.set(key, value.trim());
  return query;
}

/** 管理员素材库单独使用跨来源只读 API，不复用临时附件或会话素材接口。 */
export class MaterialLibraryApi extends ApiClient {
  /** 页码与旧游标二选一，翻页不重复请求统计或原件。 */
  list(filters: MaterialLibraryFilters, cursor?: string, signal?: AbortSignal, pagination?: {page: number; limit: number}) {
    const query = libraryQuery(filters);
    query.set('limit', String(pagination?.limit ?? 20));
    if (pagination) query.set('page', String(pagination.page));
    if (cursor) query.set('cursor', cursor);
    return this.request<Page<LibraryMaterial>>(`material-library?${query}`, 'GET', undefined, signal);
  }

  /** 总条数独立加载，慢统计不阻塞列表和预览。 */
  count(filters: MaterialLibraryFilters, signal: AbortSignal) {
    return this.request<{total: number}>(`material-library/count?${libraryQuery(filters)}`, 'GET', undefined, signal);
  }

  /** 详情读取完整说明，原件仍需独立认证。 */
  detail(id: string, signal: AbortSignal) {
    return this.request<LibraryMaterial>(`material-library/${encodeURIComponent(id)}`, 'GET', undefined, signal);
  }

  /** 标签整组替换，空数组表示明确清空；保存失败由页面保留草稿。 */
  updateTags(id: string, tags: string[]) {
    return this.request<LibraryMaterial>(`material-library/${encodeURIComponent(id)}/tags`, 'PATCH', {tags});
  }

  /** 列表只取服务端生成的小图，不为缩略预览下载完整原件。 */
  thumbnail(id: string, signal: AbortSignal): Promise<Blob> {
    return this.readBlob(id, 'thumbnail', signal);
  }

  /** 原件只在详情展开时读取，与缩略图保持独立请求和生命周期。 */
  content(id: string, signal: AbortSignal): Promise<Blob> {
    return this.readBlob(id, 'content', signal);
  }

  /** 认证头只发给同源接口；迟到响应不再读取正文并关闭流。 */
  private async readBlob(id: string, variant: 'content' | 'thumbnail', signal: AbortSignal): Promise<Blob> {
    const response = await fetch(`/api/v1/material-library/${encodeURIComponent(id)}/${variant}`, {
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
