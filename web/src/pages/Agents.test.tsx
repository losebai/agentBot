import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { Agents } from './Agents';

it('关联未加载时保留 ID，冲突保留草稿，只提交修改字段', async () => {
  const item = {id:1,revision:1,code:'helper',name:'助手',description:'',enabled:true,model_id:90,skill_ids:[80],mcp_ids:[],tools:['memory'],system_prompt:'原提示',max_steps:3,options:{}};
  const fetcher = vi.fn(async (url, init) => init?.method==='PATCH'
    ? Response.json({message:'配置版本冲突',code:'AI_CONFIG_CONFLICT'},{status:409})
    : Response.json({items:String(url).includes('/agents?')?[item]:[],next_cursor:null}));
  vi.stubGlobal('fetch',fetcher);
  render(<Agents token="offline"/>);
  fireEvent.click(await screen.findByRole('button',{name:'编辑助手'}));
  expect(screen.getByLabelText('默认模型')).toHaveValue('90');
  fireEvent.change(screen.getByLabelText('系统提示词'),{target:{value:'尚未保存的修改'}});
  fireEvent.click(screen.getByRole('button',{name:'保存 Agent'}));
  expect(await screen.findByText('配置版本冲突')).toBeInTheDocument();
  expect(screen.getByLabelText('系统提示词')).toHaveValue('尚未保存的修改');
  await waitFor(()=>expect(JSON.parse(fetcher.mock.calls.find(([,init])=>init?.method==='PATCH')![1]!.body)).toEqual({expected_revision:1,system_prompt:'尚未保存的修改'}));
});
