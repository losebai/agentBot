import { ApiClient } from './api';
import type { WorkflowCatalog, WorkflowDetail, WorkflowPatch } from './workflow-types';

/** 工作流管理继承统一认证和错误协议，所有读写可随子页卸载取消等待。 */
export class WorkflowApi extends ApiClient {
  /** 目录只返回元数据与默认选择的运行配置版本。 */
  list(after?: string, signal?: AbortSignal) {
    return this.request<WorkflowCatalog>(`workflows${after ? `?after=${encodeURIComponent(after)}` : ''}`, 'GET', undefined, signal);
  }
  /** 管理目录使用可跳转页码，默认每页十条。 */
  page(page: number, limit: number, signal?: AbortSignal) {
    return this.request<WorkflowCatalog>(`workflows?page=${page}&limit=${limit}`, 'GET', undefined, signal);
  }
  /** 只在打开 JSON 预览时请求完整工作流内容。 */
  detail(key: string, signal?: AbortSignal) {
    return this.request<WorkflowDetail>(`workflows/${encodeURIComponent(key)}`, 'GET', undefined, signal);
  }
  /** 上传的是文件名和内容，服务端独立校验大小与工作流格式。 */
  importFile(filename: string, content: string, signal?: AbortSignal) {
    return this.request<{ imported: number }>('workflows/import', 'POST', { filename, content }, signal);
  }
  /** 使用服务端已配置目录，不接受浏览器传入文件路径。 */
  sync(signal?: AbortSignal) {
    return this.request<{ imported: number }>('workflows/sync', 'POST', undefined, signal);
  }
  /** 元数据更新沿用工作流自身版本，不使用运行配置版本。 */
  update(key: string, revision: string, values: WorkflowPatch, signal?: AbortSignal) {
    return this.request<WorkflowDetail>(`workflows/${encodeURIComponent(key)}`, 'PATCH',
      { expected_revision: revision, values }, signal);
  }
  /** 默认工作流保存在运行配置，必须提交目录读到的配置版本。 */
  setDefault(key: string, revision: number, signal?: AbortSignal) {
    return this.request<WorkflowCatalog>(`workflows/${encodeURIComponent(key)}/default`, 'POST',
      { expected_revision: revision }, signal);
  }
}
