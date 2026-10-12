import { Alert, Button } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import { AgentApi } from '../agent-api';
import { AiApi } from '../ai-api';
import { errorMessage } from '../api';
import type { AgentDraft, Skill, McpServer } from '../agent-types';
import type { AiModel } from '../ai-types';
import { DirectoryEditor, ToolNamesInput } from '../components/DirectoryEditor';

const empty:AgentDraft={name:'',code:'',description:'',enabled:false,model_id:0,system_prompt:'',tools:[],skill_ids:[],mcp_ids:[],max_steps:3,options:{}};
export function Agents({token}:{token:string}) {
  const api=useMemo(()=>new AgentApi(token),[token]);const ai=useMemo(()=>new AiApi(token),[token]);
  const actions=useMemo(()=>({list:api.listAgents.bind(api),create:api.createAgent.bind(api),update:api.updateAgent.bind(api),remove:api.deleteAgent.bind(api)}),[api]);
  const [models,setModels]=useState<AiModel[]>([]),[skills,setSkills]=useState<Skill[]>([]),[mcps,setMcps]=useState<McpServer[]>([]);
  const [cursors,setCursors]=useState<{models?:string|null;skills?:string|null;mcps?:string|null}>({});const [error,setError]=useState('');
  useEffect(()=>{let alive=true;Promise.all([ai.models(),api.listSkills(),api.listMcps()]).then(([m,s,c])=>{if(alive){setModels(m.items);setSkills(s.items);setMcps(c.items);setCursors({models:m.next_cursor,skills:s.next_cursor,mcps:c.next_cursor});}}).catch(e=>{if(alive)setError(errorMessage(e));});return()=>{alive=false;};},[api,ai]);
  async function more(kind:'models'|'skills'|'mcps'){try{if(kind==='models'){const page=await ai.models(cursors.models!);setModels([...models,...page.items]);setCursors({...cursors,models:page.next_cursor});}else if(kind==='skills'){const page=await api.listSkills(cursors.skills!);setSkills([...skills,...page.items]);setCursors({...cursors,skills:page.next_cursor});}else{const page=await api.listMcps(cursors.mcps!);setMcps([...mcps,...page.items]);setCursors({...cursors,mcps:page.next_cursor});}}catch(e){setError(errorMessage(e));}}
  return <DirectoryEditor title="Agent 管理" label="Agent" description="为助手选择模型、提示词、技能和可调用工具。" empty={empty} actions={actions} fields={(draft,set)=><>
    {error&&<Alert type="error" message={error}/>}
    <label>Agent 代码<input required pattern="[a-z][a-z0-9_-]{0,63}" value={draft.code} onChange={e=>set({...draft,code:e.target.value})}/></label>
    <label>默认模型<select required value={draft.model_id||''} onChange={e=>set({...draft,model_id:Number(e.target.value)})}><option value="">选择模型</option>{draft.model_id&&!models.some(m=>m.id===draft.model_id)&&<option value={draft.model_id}>模型 #{draft.model_id}（尚未加载）</option>}{models.map(m=><option key={m.id} value={m.id}>{m.alias}{m.enabled?'':'（停用）'}</option>)}</select></label>{cursors.models&&<Button onClick={()=>void more('models')}>加载更多模型</Button>}
    <label>系统提示词<textarea rows={7} maxLength={32000} value={draft.system_prompt} onChange={e=>set({...draft,system_prompt:e.target.value})}/></label>
    <label>工具轮次<input type="number" required min={1} max={5} value={draft.max_steps} onChange={e=>set({...draft,max_steps:Number(e.target.value)})}/></label>
    <ToolNamesInput label="允许工具" value={draft.tools} onChange={tools=>set({...draft,tools})}/><p>每行一个工具名称，例如 web_search、read_url 或 MCP 工具。QQ 工具还需当前会话授权。</p>
    <fieldset><legend>关联 Skill</legend>{draft.skill_ids.filter(id=>!skills.some(s=>s.id===id)).map(id=><label key={id} className="checkbox-label"><input type="checkbox" checked onChange={()=>set({...draft,skill_ids:draft.skill_ids.filter(v=>v!==id)})}/>Skill #{id}（尚未加载）</label>)}{skills.map(s=><label key={s.id} className="checkbox-label"><input type="checkbox" checked={draft.skill_ids.includes(s.id)} onChange={e=>set({...draft,skill_ids:e.target.checked?[...draft.skill_ids,s.id]:draft.skill_ids.filter(id=>id!==s.id)})}/>{s.name}{s.enabled?'':'（停用）'}</label>)}{cursors.skills&&<Button onClick={()=>void more('skills')}>加载更多 Skill</Button>}</fieldset>
    <fieldset><legend>关联 MCP（最多 3 个）</legend>{draft.mcp_ids.filter(id=>!mcps.some(s=>s.id===id)).map(id=><label key={id} className="checkbox-label"><input type="checkbox" checked onChange={()=>set({...draft,mcp_ids:draft.mcp_ids.filter(v=>v!==id)})}/>MCP #{id}（尚未加载）</label>)}{mcps.filter(s=>s.category==='general').map(s=><label key={s.id} className="checkbox-label"><input type="checkbox" disabled={!draft.mcp_ids.includes(s.id)&&draft.mcp_ids.length>=3} checked={draft.mcp_ids.includes(s.id)} onChange={e=>set({...draft,mcp_ids:e.target.checked?[...draft.mcp_ids,s.id]:draft.mcp_ids.filter(id=>id!==s.id)})}/>{s.name}{s.enabled?'':'（停用）'}</label>)}{cursors.mcps&&<Button onClick={()=>void more('mcps')}>加载更多 MCP</Button>}</fieldset>
  </>}/>;
}
