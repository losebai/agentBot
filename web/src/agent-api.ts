import { ApiClient, ApiFailure } from './api';
import type { Page } from './ai-types';
import type { Agent, AgentDraft, Skill, SkillDraft, McpServer, McpDraft, AgentProbe, AgentCompletion, RunSummary, RunDetail, RunStep } from './agent-types';
import { streamAgent } from './agent-stream';
import { MediaApi } from './media-api';
import { validateArtifact } from './media-validation';

const query = (cursor?: string) => `?limit=50${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`;
/** Agent 与附件共享当前登录凭据，各自使用独立接口。 */
export class AgentApi extends ApiClient {
  readonly media:MediaApi;
  /** 附件客户端随登录凭据更新，不缓存旧会话令牌。 */
  constructor(token:string){super(token);this.media=new MediaApi(token);}
  listAgents(cursor?: string) { return this.request<Page<Agent>>(`agents${query(cursor)}`); }
  getAgent(id: number) { return this.request<Agent>(`agents/${id}`); }
  createAgent(body: AgentDraft) { return this.request<Agent>('agents','POST',body); }
  updateAgent(id: number, revision: number, body: Partial<AgentDraft>) { return this.request<Agent>(`agents/${id}`,'PATCH',{expected_revision:revision,...body}); }
  deleteAgent(id: number, revision: number) { return this.request<void>(`agents/${id}`,'DELETE',{expected_revision:revision}); }
  listSkills(cursor?: string) { return this.request<Page<Skill>>(`skills${query(cursor)}`); }
  getSkill(id: number) { return this.request<Skill>(`skills/${id}`); }
  createSkill(body: SkillDraft) { return this.request<Skill>('skills','POST',body); }
  updateSkill(id: number, revision: number, body: Partial<SkillDraft>) { return this.request<Skill>(`skills/${id}`,'PATCH',{expected_revision:revision,...body}); }
  deleteSkill(id: number, revision: number) { return this.request<void>(`skills/${id}`,'DELETE',{expected_revision:revision}); }
  listMcps(cursor?: string) { return this.request<Page<McpServer>>(`mcp-servers${query(cursor)}`); }
  getMcp(id: number) { return this.request<McpServer>(`mcp-servers/${id}`); }
  createMcp(body: McpDraft) { return this.request<McpServer>('mcp-servers','POST',body); }
  updateMcp(id: number, revision: number, body: Partial<McpDraft>) { return this.request<McpServer>(`mcp-servers/${id}`,'PATCH',{expected_revision:revision,...body}); }
  deleteMcp(id: number, revision: number) { return this.request<void>(`mcp-servers/${id}`,'DELETE',{expected_revision:revision}); }
  /** 同步输出同样拒绝任意 URL 和非法附件。 */
  async completeAgent(body: AgentProbe, signal?: AbortSignal) {const result=await this.request<AgentCompletion>('agent-runs','POST',body,signal);const assets=result.attachments??[];if(!Array.isArray(assets))throw new Error('附件格式无效');assets.forEach(validateArtifact);return result;}
  /** 认证后流式执行，正文和附件都经过事件校验。 */
  async *streamAgent(body: AgentProbe, signal: AbortSignal) {
    const response=await fetch('/api/v1/agent-runs/stream',{method:'POST',signal,
      headers:{Authorization:`Bearer ${this.token}`,'Content-Type':'application/json'},body:JSON.stringify(body)});
    if(!response.ok){const value=await response.json();throw new ApiFailure(value.message||'Agent 请求失败',response.status,value.code,value.request_id);}
    yield* streamAgent(response,signal);
  }
  listRuns(filters: {session_id?: string; status?: string; cursor?: string} = {}) {
    const params=new URLSearchParams({limit:'50'});
    for(const [key,value] of Object.entries(filters)) if(value) params.set(key,value);
    return this.request<Page<RunSummary>>(`agent-runs?${params}`);
  }
  /** 页面退出时取消详情读取。 */
  getRun(id: string,signal?:AbortSignal) { return this.request<RunDetail>(`agent-runs/${encodeURIComponent(id)}`,'GET',undefined,signal); }
  /** 工具步骤分页与详情共用页面取消信号。 */
  listSteps(id: string, cursor?: string,signal?:AbortSignal) { return this.request<Page<RunStep>>(`agent-runs/${encodeURIComponent(id)}/steps${query(cursor)}`,'GET',undefined,signal); }
}
