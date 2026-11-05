import { useMemo } from 'react';
import { AgentApi } from '../agent-api';
import type { SkillDraft } from '../agent-types';
import { DirectoryEditor, ToolNamesInput } from '../components/DirectoryEditor';

const empty:SkillDraft={name:'',description:'',enabled:false,category:'text',system_prompt:'',sample_prompt:'',tools:[]};
export function Skills({token}:{token:string}) {
  const actions=useMemo(()=>{const api=new AgentApi(token);return {list:api.skillPage.bind(api),create:api.createSkill.bind(api),update:api.updateSkill.bind(api),remove:api.deleteSkill.bind(api)};},[token]);
  return <DirectoryEditor title="Skill 管理" label="Skill" description="组织提示词与示例，工具依赖需由 Agent 授权。" empty={empty} actions={actions} fields={(draft,set)=><>
    <label>分类<input required maxLength={64} value={draft.category} onChange={e=>set({...draft,category:e.target.value})}/></label>
    <label>系统提示词<textarea rows={6} maxLength={16000} value={draft.system_prompt} onChange={e=>set({...draft,system_prompt:e.target.value})}/></label>
    <label>示例提示词<textarea rows={5} maxLength={32000} value={draft.sample_prompt} onChange={e=>set({...draft,sample_prompt:e.target.value})}/></label>
    <ToolNamesInput label="依赖工具" value={draft.tools} onChange={tools=>set({...draft,tools})}/><p>每行一个工具名称。清空表示没有工具依赖。</p>
  </>}/>;
}
