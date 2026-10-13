import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { McpServers } from './McpServers';

it('数据库令牌不回填，其他字段更新保留凭据，白名单不可为空',async()=>{
  const item={id:1,revision:1,name:'资料库',code:'library',description:'',enabled:true,category:'general',transport:'sse',endpoint:'http://localhost:9000/sse',credential_set:true,credential_available:true,allowed_tools:['lookup'],search_tool:null,timeout_seconds:10,required:false};
  const fetcher=vi.fn(async (_url,init)=>Response.json(init?.method==='PATCH'?{...item,required:true}:{items:[item],next_cursor:null}));
  vi.stubGlobal('fetch',fetcher);render(<McpServers token="offline"/>);
  fireEvent.click(await screen.findByRole('button',{name:'编辑资料库'}));
  expect(screen.getByLabelText('访问令牌')).toHaveValue('');
  expect(screen.queryByText('actual-secret')).not.toBeInTheDocument();
  fireEvent.click(screen.getByLabelText('必需连接'));
  fireEvent.change(screen.getByLabelText('工具白名单'),{target:{value:''}});
  expect(screen.getByRole('button',{name:'保存 MCP'})).toBeDisabled();
  fireEvent.change(screen.getByLabelText('工具白名单'),{target:{value:'lookup'}});
  fireEvent.click(screen.getByRole('button',{name:'保存 MCP'}));
  await waitFor(()=>expect(JSON.parse(fetcher.mock.calls.find(([,init])=>init?.method==='PATCH')![1]!.body)).toEqual({expected_revision:1,required:true}));
});

it('替换令牌直接写入数据库，清除必须明确勾选',async()=>{
  const item={id:1,revision:1,name:'资料库',code:'library',description:'',enabled:true,category:'general',transport:'streamable',endpoint:'http://localhost:9000/mcp',credential_set:true,credential_available:true,allowed_tools:['lookup'],search_tool:null,timeout_seconds:10,required:false};
  const fetcher=vi.fn(async (_url,init)=>Response.json(init?.method==='PATCH'?{...item,revision:2}:{items:[item],next_cursor:null}));
  vi.stubGlobal('fetch',fetcher);render(<McpServers token="offline"/>);
  fireEvent.click(await screen.findByRole('button',{name:'编辑资料库'}));
  fireEvent.change(screen.getByLabelText('访问令牌'),{target:{value:'replacement-token'}});
  fireEvent.click(screen.getByRole('button',{name:'保存 MCP'}));
  await waitFor(()=>expect(JSON.parse(fetcher.mock.calls.find(([,init])=>init?.method==='PATCH')![1]!.body)).toEqual({expected_revision:1,credential:'replacement-token'}));
  fireEvent.click(await screen.findByRole('button',{name:'编辑资料库'}));
  expect(screen.getByLabelText('访问令牌')).toHaveValue('');
  fireEvent.click(screen.getByLabelText('清除访问令牌并改为无需鉴权'));
  fireEvent.click(screen.getByRole('button',{name:'保存 MCP'}));
  await waitFor(()=>expect(fetcher.mock.calls.filter(([,init])=>init?.method==='PATCH')).toHaveLength(2));
  expect(JSON.parse(fetcher.mock.calls.filter(([,init])=>init?.method==='PATCH')[1][1]!.body).credential).toBeNull();
});
