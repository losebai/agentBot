import { Alert, Button } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import { AgentApi } from '../agent-api';
import { errorMessage } from '../api';
import type { AgentDraft, Skill } from '../agent-types';
import { AgentModelSelect } from '../components/AgentModelSelect';
import { AgentToolSelects } from '../components/AgentToolSelects';
import { DirectoryEditor } from '../components/DirectoryEditor';

const empty:AgentDraft={name:'',code:'',description:'',enabled:false,model_id:0,vision_model_id:null,fallback_model_id:null,system_prompt:'',tools:[],skill_ids:[],mcp_ids:[],options:{}};
/** 管理消息模型路由及显式错误接管；工具和 MCP 权限不因模型切换扩大。 */
export function Agents({token}:{token:string}) {
  const api=useMemo(()=>new AgentApi(token),[token]);
  const actions=useMemo(()=>({list:api.listAgents.bind(api),create:api.createAgent.bind(api),update:api.updateAgent.bind(api),remove:api.deleteAgent.bind(api)}),[api]);
  const [skills,setSkills]=useState<Skill[]>([]);
  const [cursors,setCursors]=useState<{skills?:string|null}>({});const [error,setError]=useState('');
  useEffect(()=>{let alive=true;api.listSkills().then(s=>{if(alive){setSkills(s.items);setCursors({skills:s.next_cursor});}}).catch(e=>{if(alive)setError(errorMessage(e));});return()=>{alive=false;};},[api]);
  /** 按需追加关联目录，保留草稿中尚未加载的关联 ID。 */
  async function more(){try{const page=await api.listSkills(cursors.skills!);setSkills([...skills,...page.items]);setCursors({...cursors,skills:page.next_cursor});}catch(e){setError(errorMessage(e));}}
  const directory = <DirectoryEditor title="Agent 管理" label="Agent" description="为助手配置聊天与图片理解模型、错误转发、提示词和工具。" empty={empty} actions={actions} fields={(draft,set)=><>
    {error&&<Alert type="error" message={error}/>}
    <label>Agent 代码<input required pattern="[a-z][a-z0-9_-]{0,63}" value={draft.code} onChange={e=>set({...draft,code:e.target.value})}/></label>
    <AgentModelSelect token={token} value={draft.model_id}
      visionValue={draft.vision_model_id ?? null} fallbackValue={draft.fallback_model_id ?? null}
      onChange={model_id=>set(previous=>({...previous,model_id}))}
      onVisionChange={vision_model_id=>set(previous=>({...previous,vision_model_id}))}
      onFallbackChange={fallback_model_id=>set(previous=>({...previous,fallback_model_id}))}/>
    <label>系统提示词<textarea rows={7} maxLength={32000} value={draft.system_prompt} onChange={e=>set({...draft,system_prompt:e.target.value})}/></label>
    {/* 次数预算统一由机器人配置管理，预设不再设置独立轮次。 */}
    <Alert type="info" message="工具调用次数在“机器人配置 → LLM设置”中统一调整。"/>
    <AgentToolSelects token={token} tools={draft.tools} mcpIds={draft.mcp_ids}
      onToolsChange={tools=>set(previous=>({...previous,tools}))}
      onMcpsChange={mcp_ids=>set(previous=>({...previous,mcp_ids}))}/>
    <fieldset><legend>关联 Skill</legend>{draft.skill_ids.filter(id=>!skills.some(s=>s.id===id)).map(id=><label key={id} className="checkbox-label"><input type="checkbox" checked onChange={()=>set({...draft,skill_ids:draft.skill_ids.filter(v=>v!==id)})}/>Skill #{id}（尚未加载）</label>)}{skills.map(s=><label key={s.id} className="checkbox-label"><input type="checkbox" checked={draft.skill_ids.includes(s.id)} onChange={e=>set({...draft,skill_ids:e.target.checked?[...draft.skill_ids,s.id]:draft.skill_ids.filter(id=>id!==s.id)})}/>{s.name}{s.enabled?'':'（停用）'}</label>)}{cursors.skills&&<Button onClick={()=>void more()}>加载更多 Skill</Button>}</fieldset>
  </>}/>;
  return directory;
}
