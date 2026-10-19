import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { Agents } from './Agents';

it('三种角色允许复用同一模型并说明图片模型留空的影响', async () => {
  let patch: unknown;
  const agent = { id: 1, revision: 1, code: 'helper', name: '助手', description: '', enabled: true,
    model_id: 1, vision_model_id: null, fallback_model_id: null,
    skill_ids: [], mcp_ids: [], tools: [], system_prompt: '原提示', max_steps: 3, options: {} };
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    if (init?.method === 'PATCH') { patch = JSON.parse(String(init.body)); return Response.json(agent); }
    if (String(url).includes('/ai/models?')) return Response.json({ items: [
      { id: 1, alias: 'chat', enabled: true, media: { vision_enabled: false } },
      { id: 2, alias: 'shared', enabled: true, media: { vision_enabled: true } },
    ], next_cursor: null });
    return Response.json({ items: String(url).includes('/agents?') ? [agent] : [], next_cursor: null });
  }));
  render(<Agents token="offline" />);
  fireEvent.click(await screen.findByRole('button', { name: '编辑助手' }));
  await waitFor(() => expect(screen.getByLabelText('聊天模型')).toBeEnabled());
  await waitFor(() => expect(screen.getByText(/聊天模型、图片理解模型和错误转发模型可以选择同一个模型/)).toBeVisible());
  expect(within(screen.getByLabelText('图片理解模型')).getByRole('option', {
    name: '未配置：带图请求将报错',
  })).toBeInTheDocument();
  expect(screen.getByText(/带图请求必须配置图片理解模型/)).toBeVisible();
  for (const label of ['聊天模型', '图片理解模型', '错误转发模型']) {
    fireEvent.change(screen.getByLabelText(label), { target: { value: '2' } });
  }
  fireEvent.click(screen.getByRole('button', { name: '保存 Agent' }));
  await waitFor(() => expect(patch).toEqual({ expected_revision: 1,
    model_id: 2, vision_model_id: 2, fallback_model_id: 2 }));
});

it('422 中文保存错误移入视口并保留模型与提示词草稿供重试', async () => {
  const patches: unknown[] = [];
  const agent = { id: 1, revision: 1, code: 'helper', name: '助手', description: '', enabled: true,
    model_id: 1, vision_model_id: null, fallback_model_id: null,
    skill_ids: [], mcp_ids: [], tools: [], system_prompt: '原提示', max_steps: 3, options: {} };
  const message = '所选模型尚未启用图片理解能力，请在“供应商与模型”中开启后重试';
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    if (init?.method === 'PATCH') {
      patches.push(JSON.parse(String(init.body)));
      return patches.length === 1
        ? Response.json({ code: 'MEDIA_VISION_UNSUPPORTED', message }, { status: 422 })
        : Response.json(agent);
    }
    if (String(url).includes('/ai/models?')) return Response.json({ items: [
      { id: 1, alias: 'chat', enabled: true, media: { vision_enabled: false } },
      { id: 2, alias: 'shared', enabled: true, media: { vision_enabled: true } },
    ], next_cursor: null });
    return Response.json({ items: String(url).includes('/agents?') ? [agent] : [], next_cursor: null });
  }));
  render(<Agents token="offline" />);
  fireEvent.click(await screen.findByRole('button', { name: '编辑助手' }));
  await waitFor(() => expect(screen.getByLabelText('聊天模型')).toBeEnabled());
  for (const label of ['聊天模型', '图片理解模型', '错误转发模型']) {
    fireEvent.change(screen.getByLabelText(label), { target: { value: '2' } });
  }
  fireEvent.change(screen.getByLabelText('系统提示词'), { target: { value: '保留用户编写的提示词' } });
  const save = screen.getByRole('button', { name: '保存 Agent' });
  save.focus();
  fireEvent.click(save);
  const dialog = screen.getByRole('dialog');
  expect(await within(dialog).findByText(message)).toBeVisible();
  // 保存错误获得焦点，浏览器会把长表单上方的错误滚入视口。
  expect(within(dialog).getByRole('group', { name: '操作错误' })).toHaveFocus();
  for (const label of ['聊天模型', '图片理解模型', '错误转发模型']) {
    expect(within(dialog).getByLabelText(label)).toHaveValue('2');
  }
  expect(within(dialog).getByLabelText('系统提示词')).toHaveValue('保留用户编写的提示词');
  fireEvent.click(within(dialog).getByRole('button', { name: '保存 Agent' }));
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  expect(patches).toEqual([
    { expected_revision: 1, model_id: 2, vision_model_id: 2, fallback_model_id: 2,
      system_prompt: '保留用户编写的提示词' },
    { expected_revision: 1, model_id: 2, vision_model_id: 2, fallback_model_id: 2,
      system_prompt: '保留用户编写的提示词' },
  ]);
});

it('聊天、图片理解与错误转发独立保存，清空路由提交 null', async () => {
  const patches: unknown[] = [];
  let agent = { id: 1, revision: 1, code: 'helper', name: '助手', description: '', enabled: true,
    model_id: 1, vision_model_id: null as number | null, fallback_model_id: null as number | null,
    skill_ids: [], mcp_ids: [], tools: [], system_prompt: '', max_steps: 3, options: {} };
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    if (init?.method === 'PATCH') {
      const patch = JSON.parse(String(init.body)); patches.push(patch);
      agent = { ...agent, ...patch, revision: agent.revision + 1 };
      return Response.json(agent);
    }
    if (String(url).includes('/ai/models?')) return Response.json({ items: [
      { id: 1, alias: 'chat', enabled: true, media: { vision_enabled: false } },
      { id: 2, alias: 'vision', enabled: true, media: { vision_enabled: true } },
      { id: 3, alias: 'backup', enabled: true, media: { vision_enabled: true } },
    ], next_cursor: null });
    return Response.json({ items: String(url).includes('/agents?') ? [agent] : [], next_cursor: null });
  }));
  render(<Agents token="offline" />);
  fireEvent.click(await screen.findByRole('button', { name: '编辑助手' }));
  await waitFor(() => expect(screen.getByLabelText('聊天模型')).toBeEnabled());
  fireEvent.change(screen.getByLabelText('图片理解模型'), { target: { value: '2' } });
  fireEvent.change(screen.getByLabelText('错误转发模型'), { target: { value: '3' } });
  fireEvent.click(screen.getByRole('button', { name: '保存 Agent' }));
  await waitFor(() => expect(patches).toEqual([
    { expected_revision: 1, vision_model_id: 2, fallback_model_id: 3 },
  ]));
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  fireEvent.click(screen.getByRole('button', { name: '编辑助手' }));
  await waitFor(() => expect(screen.getByLabelText('图片理解模型')).toBeEnabled());
  fireEvent.change(screen.getByLabelText('图片理解模型'), { target: { value: '' } });
  fireEvent.change(screen.getByLabelText('错误转发模型'), { target: { value: '' } });
  fireEvent.click(screen.getByRole('button', { name: '保存 Agent' }));
  await waitFor(() => expect(patches[1]).toEqual({
    expected_revision: 2, vision_model_id: null, fallback_model_id: null,
  }));
});

it('关联 MCP 只保存服务 ID，无需把远端工具写入 Agent 工具配置', async () => {
  let patch: unknown;
  const agent = { id: 1, revision: 3, code: 'helper', name: '助手', description: '', enabled: false,
    model_id: 1, vision_model_id: null, fallback_model_id: null, skill_ids: [], mcp_ids: [], tools: [], system_prompt: '原提示', max_steps: 3, options: {} };
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
  expect(screen.getByText(/已关联 MCP 的工具自动可用/)).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '保存 Agent' }));
  await waitFor(() => expect(patch).toEqual({ expected_revision: 3, mcp_ids: [8] }));
});

it('关联未加载时保留 ID，冲突保留草稿，只提交修改字段', async () => {
  const item = {id:1,revision:1,code:'helper',name:'助手',description:'',enabled:true,model_id:90,vision_model_id:null,fallback_model_id:null,skill_ids:[80],mcp_ids:[],tools:['memory'],system_prompt:'原提示',max_steps:3,options:{}};
  const fetcher = vi.fn(async (url, init) => init?.method==='PATCH'
    ? Response.json({message:'配置版本冲突',code:'AI_CONFIG_CONFLICT'},{status:409})
    : Response.json({items:String(url).includes('/agents?')?[item]:[],next_cursor:null}));
  vi.stubGlobal('fetch',fetcher);
  render(<Agents token="offline"/>);
  fireEvent.click(await screen.findByRole('button',{name:'编辑助手'}));
  expect(screen.getByLabelText('聊天模型')).toHaveValue('90');
  fireEvent.change(screen.getByLabelText('系统提示词'),{target:{value:'尚未保存的修改'}});
  fireEvent.click(screen.getByRole('button',{name:'保存 Agent'}));
  expect(await screen.findByText('配置版本冲突')).toBeInTheDocument();
  expect(screen.getByLabelText('系统提示词')).toHaveValue('尚未保存的修改');
  await waitFor(()=>expect(JSON.parse(fetcher.mock.calls.find(([,init])=>init?.method==='PATCH')![1]!.body)).toEqual({expected_revision:1,system_prompt:'尚未保存的修改'}));
});

it('重新打开编辑时读取最新模型状态，不沿用页面首次访问的停用快照', async () => {
  let enabled = false;
  const item = { id: 1, revision: 1, code: 'helper', name: '助手', description: '', enabled: true,
    model_id: 9, vision_model_id: null, fallback_model_id: null, skill_ids: [], mcp_ids: [], tools: [], system_prompt: '', max_steps: 3, options: {} };
  vi.stubGlobal('fetch', vi.fn(async url => {
    if (String(url).includes('/ai/models?')) return Response.json({ items: [
      { id: 9, revision: 1, alias: 'local-model', upstream_model: 'qwen', provider_id: 1, enabled },
    ], next_cursor: null });
    return Response.json({ items: String(url).includes('/agents?') ? [item] : [], next_cursor: null });
  }));
  render(<Agents token="offline" />);
  fireEvent.click(await screen.findByRole('button', { name: '编辑助手' }));
  await waitFor(() => expect(screen.getByLabelText('聊天模型')).toBeEnabled());
  expect(within(screen.getByLabelText('聊天模型')).queryByRole('option', { name: /local-model/ })).not.toBeInTheDocument();
  await waitFor(() => expect(screen.getByText('聊天模型已停用，请重新选择已启用的模型。')).toBeVisible());
  // 保留已有绑定，不因隐藏停用项而自动换成其他模型。
  expect(screen.getByLabelText('聊天模型')).toHaveValue('9');
  fireEvent.click(screen.getByRole('button', { name: /取\s*消/ }));
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  // 对应切换至供应商页面启用模型，再回到仍挂载着的 Agent 页面。
  enabled = true;
  fireEvent.click(screen.getByRole('button', { name: '编辑助手' }));
  expect(await within(screen.getByLabelText('聊天模型')).findByRole('option', { name: 'local-model' })).toBeInTheDocument();
  expect(screen.getByLabelText('聊天模型')).toHaveValue('9');
  expect(within(screen.getByLabelText('聊天模型')).queryByRole('option', { name: /local-model（停用）/ })).not.toBeInTheDocument();
});

it('模型翻页保留已选 ID，刷新失败明确报错而不把未知状态显示为停用', async () => {
  let failed = false;
  const item = { id: 1, revision: 1, code: 'helper', name: '助手', description: '', enabled: true,
    model_id: 90, vision_model_id: null, fallback_model_id: null, skill_ids: [], mcp_ids: [], tools: [], system_prompt: '', max_steps: 3, options: {} };
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
  expect(await within(screen.getByLabelText('聊天模型')).findByRole('option', { name: 'enabled-model' })).toBeInTheDocument();
  for (const label of ['聊天模型', '图片理解模型', '错误转发模型']) {
    expect(within(screen.getByLabelText(label)).queryByRole('option', { name: /disabled-model/ })).not.toBeInTheDocument();
  }
  expect(screen.getByLabelText('聊天模型')).toHaveValue('90');
  failed = true;
  fireEvent.click(screen.getByRole('button', { name: '刷新模型状态' }));
  expect(await screen.findByText(/模型列表读取失败：目录暂时不可用/)).toBeVisible();
  expect(screen.queryByRole('option', { name: /停用/ })).not.toBeInTheDocument();
  expect(screen.getByLabelText('聊天模型')).toHaveValue('90');
  expect(screen.getByLabelText('系统提示词')).toHaveValue('保留草稿');
});
