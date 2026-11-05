import { ApiClient, ApiFailure } from './api';
import type { CatalogPage, Page } from './ai-types';
import type { Agent, AgentDraft, Skill, SkillDraft, McpServer, McpDraft, McpTool, AgentProbe, AgentCompletion, RunSummary, RunDetail, RunStep } from './agent-types';
import { streamAgent } from './agent-stream';
import { MediaApi } from './media-api';
import { validateArtifact } from './media-validation';

const query = (cursor?: string) => `?limit=50${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`;
/** Agent 与附件共享当前登录凭据，各自使用独立接口。 */
export class AgentApi extends ApiClient {
  readonly media:MediaApi;
  /** 附件客户端随登录凭据更新，不缓存旧会话令牌。 */
  constructor(token:string){super(token);this.media=new MediaApi(token);}
  /** 页面刷新或离开时取消旧目录查询，避免晚到响应覆盖当前选择。 */
  listAgents(cursor?: string, signal?: AbortSignal) { return this.request<Page<Agent>>(`agents${query(cursor)}`, 'GET', undefined, signal); }
  agentPage(page: number, limit: number, signal?: AbortSignal) { return this.request<CatalogPage<Agent>>(`agents?page=${page}&limit=${limit}`, 'GET', undefined, signal); }
  getAgent(id: number, signal?: AbortSignal) { return this.request<Agent>(`agents/${id}`, 'GET', undefined, signal); }
  createAgent(body: AgentDraft) { return this.request<Agent>('agents','POST',body); }
  updateAgent(id: number, revision: number, body: Partial<AgentDraft>) { return this.request<Agent>(`agents/${id}`,'PATCH',{expected_revision:revision,...body}); }
  deleteAgent(id: number, revision: number) { return this.request<void>(`agents/${id}`,'DELETE',{expected_revision:revision}); }
  listSkills(cursor?: string) { return this.request<Page<Skill>>(`skills${query(cursor)}`); }
  skillPage(page: number, limit: number) { return this.request<CatalogPage<Skill>>(`skills?page=${page}&limit=${limit}`); }
  getSkill(id: number) { return this.request<Skill>(`skills/${id}`); }
  createSkill(body: SkillDraft) { return this.request<Skill>('skills','POST',body); }
  updateSkill(id: number, revision: number, body: Partial<SkillDraft>) { return this.request<Skill>(`skills/${id}`,'PATCH',{expected_revision:revision,...body}); }
  deleteSkill(id: number, revision: number) { return this.request<void>(`skills/${id}`,'DELETE',{expected_revision:revision}); }
  /** 编辑弹窗关闭时取消目录读取，避免继续加载无用分页。 */
  listMcps(cursor?: string, signal?: AbortSignal) { return this.request<Page<McpServer>>(`mcp-servers${query(cursor)}`, 'GET', undefined, signal); }
  mcpPage(page: number, limit: number) { return this.request<CatalogPage<McpServer>>(`mcp-servers?page=${page}&limit=${limit}`); }
  getMcp(id: number) { return this.request<McpServer>(`mcp-servers/${id}`); }
  /** 只读取已保存服务的工具定义；取消时由后端释放 MCP 连接。 */
  listMcpTools(id: number, signal: AbortSignal) { return this.request<McpTool[]>(`mcp-servers/${id}/tools`, 'GET', undefined, signal); }
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
  listRuns(filters: {session_id?: string; status?: string; cursor?: string} = {}, signal?: AbortSignal) {
    const params=new URLSearchParams({limit:'10'});
    for(const [key,value] of Object.entries(filters)) if(value) params.set(key,value);
    return this.request<CatalogPage<RunSummary>>(`agent-runs?${params}`, 'GET', undefined, signal);
  }
  /** 运行记录管理页按页码读取，筛选总数由后端同口径计算。 */
  runPage(filters: {session_id?: string; status?: string}, page: number, limit: number, signal?: AbortSignal) {
    const params=new URLSearchParams({page:String(page),limit:String(limit)});
    for(const [key,value] of Object.entries(filters)) if(value) params.set(key,value);
    return this.request<CatalogPage<RunSummary>>(`agent-runs?${params}`, 'GET', undefined, signal);
  }
  /** 页面退出时取消详情读取。 */
  getRun(id: string,signal?:AbortSignal) { return this.request<RunDetail>(`agent-runs/${encodeURIComponent(id)}`,'GET',undefined,signal); }
  /** 工具步骤分页与详情共用页面取消信号。 */
  listSteps(id: string, cursor?: string,signal?:AbortSignal) { return this.request<CatalogPage<RunStep>>(`agent-runs/${encodeURIComponent(id)}/steps${query(cursor)}`,'GET',undefined,signal); }
  /** 详情中的步骤独立分页，避免长工具链一次渲染全部负载。 */
  stepPage(id:string,page:number,limit:number,signal?:AbortSignal){return this.request<CatalogPage<RunStep>>(`agent-runs/${encodeURIComponent(id)}/steps?page=${page}&limit=${limit}`,'GET',undefined,signal);}
}
