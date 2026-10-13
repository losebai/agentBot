import { useMemo } from 'react';
import { AgentApi } from '../agent-api';
import type { McpDraft } from '../agent-types';
import { CredentialInput } from '../components/CredentialInput';
import { DirectoryEditor, ToolNamesInput } from '../components/DirectoryEditor';

const empty:McpDraft={name:'',code:'',description:'',enabled:false,category:'general',transport:'streamable',endpoint:'',allowed_tools:[],search_tool:null,timeout_seconds:10,required:false};
// 每个 MCP 在数据库中保存独立连接配置，令牌只通过写入表单提交。
export function McpServers({token}:{token:string}) {
  const actions=useMemo(()=>{const api=new AgentApi(token);return {list:api.listMcps.bind(api),create:api.createMcp.bind(api),update:api.updateMcp.bind(api),remove:api.deleteMcp.bind(api)};},[token]);
  return <DirectoryEditor title="MCP 服务" label="MCP" description="登记可用服务、连接协议和工具白名单。" empty={empty} actions={actions} valid={d=>d.allowed_tools.length>0&&(d.category!=='search'||Boolean(d.search_tool))} fields={(draft,set,item)=><>
    <label>服务代码<input required pattern="[a-z][a-z0-9_-]{0,63}" value={draft.code} onChange={e=>set({...draft,code:e.target.value})}/></label>
    <label>分类<select value={draft.category} onChange={e=>set({...draft,category:e.target.value as McpDraft['category']})}><option value="general">通用工具</option><option value="search">网页搜索</option></select></label>
    <label>连接协议<select value={draft.transport} onChange={e=>set({...draft,transport:e.target.value as McpDraft['transport']})}><option value="streamable">Streamable HTTP</option><option value="sse">SSE</option></select></label>
    <label>服务地址<input required type="url" maxLength={512} value={draft.endpoint} onChange={e=>set({...draft,endpoint:e.target.value})}/></label>
    <CredentialInput label="访问令牌" value={draft.credential} onChange={credential=>set({...draft,credential})} editing={Boolean(item)} configured={item?.credential_set||false} available={item?.credential_available??true}/>
    <ToolNamesInput label="工具白名单" required value={draft.allowed_tools} onChange={allowed_tools=>set({...draft,allowed_tools})}/><p>每行一个工具名；* 允许此服务提供的全部工具（仍受数量与参数限制）。</p>
    {draft.category==='search'&&<label>搜索工具名称<input required value={draft.search_tool||''} onChange={e=>set({...draft,search_tool:e.target.value||null})}/></label>}
    <label>连接超时（秒）<input required type="number" min={1} max={30} value={draft.timeout_seconds} onChange={e=>set({...draft,timeout_seconds:Number(e.target.value)})}/></label>
    <label className="checkbox-label"><input type="checkbox" checked={draft.required} onChange={e=>set({...draft,required:e.target.checked})}/>必需连接</label><p>非必需服务仅在连接超时时跳过；鉴权或协议错误仍会终止请求。</p>
  </>}/>;
}
