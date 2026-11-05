import { ChoiceSelect } from '../components/ChoiceSelect';
import { useMemo } from 'react';
import { AgentApi } from '../agent-api';
import type { McpDraft } from '../agent-types';
import { CredentialInput } from '../components/CredentialInput';
import { DirectoryEditor, ToolNamesInput } from '../components/DirectoryEditor';
import { McpTools } from '../components/McpTools';

const empty:McpDraft={name:'',code:'',description:'',enabled:false,category:'general',transport:'streamable',endpoint:'',allowed_tools:['*'],search_tool:null,timeout_seconds:10,required:false};
/** 远程服务使用 MCP 协议，独立连接配置保存在数据库，令牌只通过表单提交。 */
export function McpServers({token}:{token:string}) {
  const api=useMemo(()=>new AgentApi(token),[token]);
  const actions=useMemo(()=>({list:api.mcpPage.bind(api),create:api.createMcp.bind(api),update:api.updateMcp.bind(api),remove:api.deleteMcp.bind(api)}),[api]);
  return <DirectoryEditor title="远程服务" label="远程服务" description="通过 MCP 协议登记独立远程服务，获取完整工具列表并管理每个工具的启用状态。搜索类服务也通过关联服务使用其原生工具。" empty={empty} actions={actions} fields={(draft,set,item)=><>
    <label>服务代码<input required pattern="[a-z][a-z0-9_-]{0,63}" value={draft.code} onChange={e=>set({...draft,code:e.target.value})}/></label>
    <label>分类<ChoiceSelect aria-label="分类" value={draft.category} onChange={e=>set({...draft,category:e as McpDraft['category']})}><option value="general">通用工具</option><option value="search">网页搜索</option></ChoiceSelect></label>
    <label>连接协议<ChoiceSelect aria-label="连接协议" value={draft.transport} onChange={e=>set({...draft,transport:e as McpDraft['transport']})}><option value="streamable">Streamable HTTP</option><option value="sse">SSE</option></ChoiceSelect></label>
    <label>服务地址<input required type="url" maxLength={512} value={draft.endpoint} onChange={e=>set({...draft,endpoint:e.target.value})}/></label>
    <CredentialInput label="访问令牌" value={draft.credential} onChange={credential=>set({...draft,credential})} editing={Boolean(item)} configured={item?.credential_set||false} available={item?.credential_available??true}/>
    <McpTools api={api} server={item} allowedTools={draft.allowed_tools}
      connectionChanged={Boolean(item&&(draft.endpoint!==item.endpoint||draft.transport!==item.transport||draft.timeout_seconds!==item.timeout_seconds||draft.credential!==undefined))}
      onChange={allowed_tools=>set(previous=>({...previous,allowed_tools}))}/>
    <ToolNamesInput label="工具白名单" value={draft.allowed_tools} onChange={allowed_tools=>set({...draft,allowed_tools})}/><p>每行一个服务端原生工具名；* 允许全部工具，留空关闭全部工具。分类不改变工具名称和参数，也不绑定内置工具。</p>
    <label>连接超时（秒）<input required type="number" min={1} max={30} value={draft.timeout_seconds} onChange={e=>set({...draft,timeout_seconds:Number(e.target.value)})}/></label>
    <label className="checkbox-label"><input type="checkbox" checked={draft.required} onChange={e=>set({...draft,required:e.target.checked})}/>必需连接</label><p>非必需服务仅在连接超时时跳过；鉴权或协议错误仍会终止请求。</p>
  </>}/>;
}
