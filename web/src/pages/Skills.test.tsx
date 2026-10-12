import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { Skills } from './Skills';

it('明确清空 Skill 工具依赖',async()=>{
  const item={id:1,revision:1,name:'资料整理',description:'',enabled:true,category:'text',system_prompt:'',sample_prompt:'',tools:['read_url']};
  const fetcher=vi.fn(async (_url,init)=>Response.json(init?.method==='PATCH'?{...item,tools:[]}:{items:[item],next_cursor:null}));
  vi.stubGlobal('fetch',fetcher);render(<Skills token="offline"/>);
  fireEvent.click(await screen.findByRole('button',{name:'编辑资料整理'}));
  fireEvent.change(screen.getByLabelText('依赖工具'),{target:{value:''}});
  fireEvent.click(screen.getByRole('button',{name:'保存 Skill'}));
  await waitFor(()=>expect(JSON.parse(fetcher.mock.calls.find(([,init])=>init?.method==='PATCH')![1]!.body)).toEqual({expected_revision:1,tools:[]}));
});
