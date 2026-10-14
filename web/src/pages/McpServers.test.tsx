import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { McpServers } from './McpServers';

it('数据库令牌不回填，其他字段更新保留凭据，空集合表示关闭全部工具',async()=>{
  const item={id:1,revision:1,name:'资料库',code:'library',description:'',enabled:true,category:'general',transport:'sse',endpoint:'http://localhost:9000/sse',credential_set:true,credential_available:true,allowed_tools:['lookup'],search_tool:null,timeout_seconds:10,required:false};
  const fetcher=vi.fn(async (_url,init)=>Response.json(init?.method==='PATCH'?{...item,required:true}:{items:[item],next_cursor:null}));
  vi.stubGlobal('fetch',fetcher);render(<McpServers token="offline"/>);
  fireEvent.click(await screen.findByRole('button',{name:'编辑资料库'}));
  expect(screen.getByLabelText('访问令牌')).toHaveValue('');
  expect(screen.queryByText('actual-secret')).not.toBeInTheDocument();
  fireEvent.click(screen.getByLabelText('必需连接'));
  fireEvent.change(screen.getByLabelText('工具白名单'),{target:{value:''}});
  expect(screen.getByRole('button',{name:'保存 MCP'})).toBeEnabled();
  fireEvent.change(screen.getByLabelText('工具白名单'),{target:{value:'lookup'}});
  fireEvent.click(screen.getByRole('button',{name:'保存 MCP'}));
  await waitFor(()=>expect(JSON.parse(fetcher.mock.calls.find(([,init])=>init?.method==='PATCH')![1]!.body)).toEqual({expected_revision:1,required:true}));
});

it('获取全部工具后可单独关闭工具并保存集合，不执行服务端工具', async () => {
  const item = {id:2,revision:3,name:'Windows',code:'windows',description:'',enabled:true,category:'general',transport:'streamable',endpoint:'http://localhost:8000/mcp',credential_set:false,credential_available:true,allowed_tools:['*'],search_tool:null,timeout_seconds:10,required:false};
  const fetcher = vi.fn(async (url, init) => String(url).endsWith('/2/tools')
    ? Response.json([
      {name:'Snapshot',description:'读取桌面状态',input_schema:{type:'object'},source:'mcp'},
      {name:'Click',description:'点击控件',input_schema:{type:'object'},source:'mcp'},
    ]) : Response.json(init?.method === 'PATCH' ? {...item,revision:4} : {items:[item],next_cursor:null}));
  vi.stubGlobal('fetch', fetcher);
  render(<McpServers token="offline" />);
  fireEvent.click(await screen.findByRole('button', {name:'编辑Windows'}));
  fireEvent.click(screen.getByRole('button', {name:'获取全部工具'}));
  await waitFor(() => expect(screen.getByText('读取桌面状态')).toBeVisible());
  expect(screen.getByRole('switch', {name:'启用工具 Snapshot'})).toBeChecked();
  fireEvent.click(screen.getByRole('switch', {name:'启用工具 Click'}));
  expect(screen.getByRole('switch', {name:'启用工具 Click'})).not.toBeChecked();
  fireEvent.click(screen.getByRole('button', {name:'保存 MCP'}));
  await waitFor(() => expect(JSON.parse(fetcher.mock.calls.find(([,init])=>init?.method==='PATCH')![1]!.body)).toEqual({expected_revision:3,allowed_tools:['Snapshot']}));
});

it('工具列表获取失败可重试，关闭全部与启用全部更新草稿', async () => {
  const item = {id:2,revision:1,name:'Windows',code:'windows',description:'',enabled:true,category:'general',transport:'streamable',endpoint:'http://localhost:8000/mcp',allowed_tools:['*'],search_tool:null,timeout_seconds:10,required:false};
  let failed = true;
  vi.stubGlobal('fetch', vi.fn(async url => String(url).endsWith('/2/tools')
    ? failed ? Response.json({message:'连接暂时不可用'}, {status:503}) : Response.json([{name:'Snapshot',description:'桌面',input_schema:{type:'object'},source:'mcp'}])
    : Response.json({items:[item],next_cursor:null})));
  render(<McpServers token="offline" />);
  fireEvent.click(await screen.findByRole('button', {name:'编辑Windows'}));
  fireEvent.click(screen.getByRole('button', {name:'获取全部工具'}));
  await waitFor(() => expect(screen.getByText(/连接暂时不可用/)).toBeVisible());
  failed = false;
  fireEvent.click(screen.getByRole('button', {name:'获取全部工具'}));
  expect(await screen.findByRole('switch', {name:'启用工具 Snapshot'})).toBeChecked();
  fireEvent.click(screen.getByRole('button', {name:'关闭全部工具'}));
  expect(screen.getByLabelText('工具白名单')).toHaveValue('');
  expect(screen.getByRole('button', {name:'保存 MCP'})).toBeEnabled();
  fireEvent.click(screen.getByRole('button', {name:'启用全部工具'}));
  expect(screen.getByLabelText('工具白名单')).toHaveValue('*');
});

it('关闭弹窗取消工具发现，修改连接配置后先保存再获取', async () => {
  const item = {id:2,revision:1,name:'Windows',code:'windows',description:'',enabled:true,category:'general',transport:'streamable',endpoint:'http://localhost:8000/mcp',allowed_tools:['*'],search_tool:null,timeout_seconds:10,required:false};
  let signal: AbortSignal | undefined;
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    if (String(url).endsWith('/2/tools')) {
      signal = init?.signal as AbortSignal;
      return new Promise<Response>(() => {});
    }
    return Response.json({items:[item],next_cursor:null});
  }));
  render(<McpServers token="offline" />);
  fireEvent.click(await screen.findByRole('button', {name:'编辑Windows'}));
  fireEvent.click(screen.getByRole('button', {name:'获取全部工具'}));
  await waitFor(() => expect(signal).toBeDefined());
  fireEvent.click(screen.getByRole('button', {name:/取\s*消/}));
  await waitFor(() => expect(signal?.aborted).toBe(true));
  fireEvent.click(screen.getByRole('button', {name:'编辑Windows'}));
  fireEvent.change(screen.getByLabelText('服务地址'), {target:{value:'http://localhost:9000/mcp'}});
  expect(screen.getByRole('button', {name:'获取全部工具'})).toBeDisabled();
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
