import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { Agents } from './Agents';

it('工具和 MCP 下拉选择保存准确的名称和 ID，不改变其他 Agent 配置', async () => {
  let patch: unknown;
  const agent = { id: 1, revision: 3, code: 'helper', name: '助手', description: '', enabled: false,
    model_id: 1, skill_ids: [], mcp_ids: [], tools: [], system_prompt: '原提示', max_steps: 3, options: {} };
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    if (init?.method === 'PATCH') { patch = JSON.parse(String(init.body)); return Response.json(agent); }
    if (String(url).includes('/mcp-servers?')) return Response.json({ items: [
      { id: 8, name: '文档服务', code: 'docs', category: 'general', enabled: true, allowed_tools: ['find_docs'] },
    ], next_cursor: null });
    return Response.json({ items: String(url).includes('/agents?') ? [agent] : [], next_cursor: null });
  }));
  render(<Agents token="offline" />);
  fireEvent.click(await screen.findByRole('button', { name: '编辑助手' }));
  await waitFor(() => expect(screen.getByRole('combobox', { name: '关联 MCP（最多 3 个）' })).toBeEnabled());
  fireEvent.mouseDown(screen.getByRole('combobox', { name: '关联 MCP（最多 3 个）' }));
  fireEvent.click(await screen.findByText('文档服务 · docs'));
  fireEvent.mouseDown(screen.getByRole('combobox', { name: '允许工具' }));
  fireEvent.change(screen.getByRole('combobox', { name: '允许工具' }), { target: { value: 'find_docs' } });
  fireEvent.click(await screen.findByText('find_docs · 文档服务'));
  fireEvent.click(screen.getByRole('button', { name: '保存 Agent' }));
  await waitFor(() => expect(patch).toEqual({ expected_revision: 3, tools: ['find_docs'], mcp_ids: [8] }));
});

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

it('重新打开编辑时读取最新模型状态，不沿用页面首次访问的停用快照', async () => {
  let enabled = false;
  const item = { id: 1, revision: 1, code: 'helper', name: '助手', description: '', enabled: true,
    model_id: 9, skill_ids: [], mcp_ids: [], tools: [], system_prompt: '', max_steps: 3, options: {} };
  vi.stubGlobal('fetch', vi.fn(async url => {
    if (String(url).includes('/ai/models?')) return Response.json({ items: [
      { id: 9, revision: 1, alias: 'local-model', upstream_model: 'qwen', provider_id: 1, enabled },
    ], next_cursor: null });
    return Response.json({ items: String(url).includes('/agents?') ? [item] : [], next_cursor: null });
  }));
  render(<Agents token="offline" />);
  fireEvent.click(await screen.findByRole('button', { name: '编辑助手' }));
  expect(await screen.findByRole('option', { name: 'local-model（停用）' })).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: /取\s*消/ }));
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  // 对应切换至供应商页面启用模型，再回到仍挂载着的 Agent 页面。
  enabled = true;
  fireEvent.click(screen.getByRole('button', { name: '编辑助手' }));
  expect(await screen.findByRole('option', { name: 'local-model' })).toBeInTheDocument();
  expect(screen.getByLabelText('默认模型')).toHaveValue('9');
  expect(screen.queryByRole('option', { name: /local-model（停用）/ })).not.toBeInTheDocument();
});

it('模型翻页保留已选 ID，刷新失败明确报错而不把未知状态显示为停用', async () => {
  let failed = false;
  const item = { id: 1, revision: 1, code: 'helper', name: '助手', description: '', enabled: true,
    model_id: 90, skill_ids: [], mcp_ids: [], tools: [], system_prompt: '', max_steps: 3, options: {} };
  vi.stubGlobal('fetch', vi.fn(async url => {
    if (String(url).includes('/ai/models?')) {
      if (failed) return Response.json({ message: '目录暂时不可用' }, { status: 503 });
      return Response.json(String(url).includes('cursor=1') ? { items: [
        { id: 90, alias: 'enabled-model', upstream_model: 'qwen', provider_id: 1, enabled: true },
      ], next_cursor: null } : { items: [
        { id: 1, alias: 'disabled-model', upstream_model: 'old', provider_id: 1, enabled: false },
      ], next_cursor: '1' });
    }
    return Response.json({ items: String(url).includes('/agents?') ? [item] : [], next_cursor: null });
  }));
  render(<Agents token="offline" />);
  fireEvent.click(await screen.findByRole('button', { name: '编辑助手' }));
  fireEvent.change(screen.getByLabelText('系统提示词'), { target: { value: '保留草稿' } });
  fireEvent.click(await screen.findByRole('button', { name: '加载更多模型' }));
  expect(await screen.findByRole('option', { name: 'enabled-model' })).toBeInTheDocument();
  expect(screen.getByRole('option', { name: 'disabled-model（停用）' })).toBeInTheDocument();
  expect(screen.getByLabelText('默认模型')).toHaveValue('90');
  failed = true;
  fireEvent.click(screen.getByRole('button', { name: '刷新模型状态' }));
  expect(await screen.findByText(/模型列表读取失败：目录暂时不可用/)).toBeVisible();
  expect(screen.queryByRole('option', { name: /停用/ })).not.toBeInTheDocument();
  expect(screen.getByLabelText('默认模型')).toHaveValue('90');
  expect(screen.getByLabelText('系统提示词')).toHaveValue('保留草稿');
});
