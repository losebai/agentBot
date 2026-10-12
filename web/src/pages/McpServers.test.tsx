import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { McpServers } from './McpServers';

it('只编辑凭据引用并明确设置必需连接，白名单不可为空',async()=>{
  const item={id:1,revision:1,name:'资料库',code:'library',description:'',enabled:true,category:'general',transport:'sse',endpoint:'http://localhost:9000/sse',credential_ref:'AGENT_BOT_MCP_LIBRARY',credential_available:true,allowed_tools:['lookup'],search_tool:null,timeout_seconds:10,required:false};
  const fetcher=vi.fn(async (_url,init)=>Response.json(init?.method==='PATCH'?{...item,required:true}:{items:[item],next_cursor:null}));
  vi.stubGlobal('fetch',fetcher);render(<McpServers token="offline"/>);
  fireEvent.click(await screen.findByRole('button',{name:'编辑资料库'}));
  expect(screen.getByLabelText('凭据变量名')).toHaveValue('AGENT_BOT_MCP_LIBRARY');
  expect(screen.queryByText('actual-secret')).not.toBeInTheDocument();
  fireEvent.click(screen.getByLabelText('必需连接'));
  fireEvent.change(screen.getByLabelText('工具白名单'),{target:{value:''}});
  expect(screen.getByRole('button',{name:'保存 MCP'})).toBeDisabled();
  fireEvent.change(screen.getByLabelText('工具白名单'),{target:{value:'lookup'}});
  fireEvent.click(screen.getByRole('button',{name:'保存 MCP'}));
  await waitFor(()=>expect(JSON.parse(fetcher.mock.calls.find(([,init])=>init?.method==='PATCH')![1]!.body)).toEqual({expected_revision:1,required:true}));
});
