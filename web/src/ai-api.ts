import { ApiClient } from './api';
import type { AiModel, ModelDraft, Page, Provider, ProviderDraft, UpstreamModels, ModelImportItem, ModelImportResult, SessionDetail, SessionSummary } from './ai-types';

export class AiApi extends ApiClient {
  providers(cursor?: string) { return this.request<Page<Provider>>(`ai/providers?limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`); }
  models(cursor?: string, providerId?: number, signal?: AbortSignal) { return this.request<Page<AiModel>>(`ai/models?limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}${providerId ? `&provider_id=${providerId}` : ''}`, 'GET', undefined, signal); }
  // 模型发现只传供应商身份与版本，实际 Key 留在服务器端。
  upstreamModels(id: number, revision: number, signal?: AbortSignal) { return this.request<UpstreamModels>(`ai/providers/${id}/upstream-models?expected_revision=${revision}`, 'GET', undefined, signal); }
  importModels(id: number, revision: number, models: ModelImportItem[], signal?: AbortSignal) { return this.request<ModelImportResult>(`ai/providers/${id}/models/import`, 'POST', {expected_revision: revision, models}, signal); }
  /** 新模型直接使用上游名称作别名；已有项保留配置，同名冲突由服务端明确拒绝。 */
  async syncModels(id: number, revision: number, signal?: AbortSignal) {
    const discovered = await this.upstreamModels(id, revision, signal);
    let added = 0;
    // 导入接口每批最多 100 项；中断时停止后续批次，手动刷新可继续同步。
    for (let offset = 0; offset < discovered.models.length; offset += 100) {
      signal?.throwIfAborted();
      const models = discovered.models.slice(offset, offset + 100).map(upstream_model => ({
        upstream_model, alias: upstream_model,
      }));
      const result = await this.importModels(id, discovered.revision, models, signal);
      added += result.imported.length;
    }
    signal?.throwIfAborted();
    return { total: discovered.models.length, added };
  }
  createProvider(body: ProviderDraft) { return this.request<Provider>('ai/providers', 'POST', body); }
  updateProvider(id: number, revision: number, changes: Partial<ProviderDraft>) { return this.request<Provider>(`ai/providers/${id}`, 'PATCH', { expected_revision: revision, ...changes }); }
  deleteProvider(id: number, revision: number) { return this.request<void>(`ai/providers/${id}`, 'DELETE', { expected_revision: revision }); }
  createModel(body: ModelDraft) { return this.request<AiModel>('ai/models', 'POST', body); }
  /** 状态更新使用版本校验，面板离开时可取消等待。 */
  updateModel(id: number, revision: number, changes: Partial<ModelDraft>, signal?: AbortSignal) { return this.request<AiModel>(`ai/models/${id}`, 'PATCH', { expected_revision: revision, ...changes }, signal); }
  deleteModel(id: number, revision: number) { return this.request<void>(`ai/models/${id}`, 'DELETE', { expected_revision: revision }); }
  sessions(session: string, status: string, cursor?: string) {
    const query = new URLSearchParams({ limit: '50' });
    if (session) query.set('session_id', session);
    if (status) query.set('status', status);
    if (cursor) query.set('cursor', cursor);
    return this.request<Page<SessionSummary>>(`ai/sessions?${query}`);
  }
  session(id: string) { return this.request<SessionDetail>(`ai/sessions/${encodeURIComponent(id)}`); }
}

export function changes<T extends object>(original: T, draft: T): Partial<T> {
  const result: Partial<T> = {};
  for (const key of Object.keys(draft) as (keyof T)[]) if (original[key] !== draft[key]) result[key] = draft[key];
  return result;
}
