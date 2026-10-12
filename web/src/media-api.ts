import { ApiClient, ApiFailure } from './api';
import type { Page } from './ai-types';
import type { MediaAsset, MediaGenerationRequest, MediaGenerationResult, MediaRun, MediaRunDetail } from './media-types';

/** 文件使用原始请求体和认证头，不生成包含凭据的下载 URL。 */
export class MediaApi extends ApiClient {
  /** 保留文件 MIME，服务器负责解码验证和大小限制。 */
  async upload(file:File,sessionId:string,signal:AbortSignal):Promise<MediaAsset>{
    const response=await fetch(`/api/v1/media/uploads?session_id=${encodeURIComponent(sessionId)}`,{method:'POST',headers:{Authorization:`Bearer ${this.token}`,'Content-Type':file.type||'application/octet-stream'},body:file,signal});
    await check(response);return response.json();
  }
  /** 一次生成只有一次提交，取消不会自动重试。 */
  generate(body:MediaGenerationRequest,signal:AbortSignal){return this.request<MediaGenerationResult>('media/generations','POST',body,signal);}
  /** 查询元数据供详情页判断可用状态。 */
  asset(id:string,signal?:AbortSignal){return this.request<MediaAsset>(`media/${encodeURIComponent(id)}`,'GET',undefined,signal);}
  /** 预览组件负责撤销创建的 Blob URL。 */
  async content(id:string,signal:AbortSignal):Promise<Blob>{
    const response=await fetch(`/api/v1/media/${encodeURIComponent(id)}/content`,{headers:{Authorization:`Bearer ${this.token}`},signal});await check(response);return response.blob();
  }
  /** 游标随会话、状态筛选一起传递。 */
  runs(session:string,status:string,cursor?:string,signal?:AbortSignal){const query=new URLSearchParams({limit:'50'});if(session)query.set('session_id',session);if(status)query.set('status',status);if(cursor)query.set('cursor',cursor);return this.request<Page<MediaRun>>(`media/runs?${query}`,'GET',undefined,signal);}
  /** 读取运行详情，附件内容仍需单独授权读取。 */
  getRun(id:string,signal?:AbortSignal){return this.request<MediaRunDetail>(`media/runs/${encodeURIComponent(id)}`,'GET',undefined,signal);}
}
/** 与 JSON API 统一错误结构，兼容代理返回非 JSON 错误。 */
async function check(response:Response){if(response.ok)return;const body=await response.json().catch(()=>({}));throw new ApiFailure(body.message||'媒体请求失败',response.status,body.code||'MEDIA_FAILED',body.request_id);}
