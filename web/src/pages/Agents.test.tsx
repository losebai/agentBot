import { chooseSelect, clearSelect, selectDisplay } from '../test-select';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { Agents } from './Agents';
import { useRunningPluginFixture } from '../test-plugins';

useRunningPluginFixture();

it('三种角色允许复用同一模型并说明图片模型留空的影响', async () => {
  let patch: unknown;
  const agent = { id: 1, revision: 1, code: 'helper', name: '助手', description: '', enabled: true,
    model_id: 1, vision_model_id: null, fallback_model_id: null,
    skill_ids: [], mcp_ids: [], tools: [], system_prompt: '原提示', max_steps: 3, options: {} };
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    if (init?.method === 'PATCH') { patch = JSON.parse(String(init.body)); return Response.json(agent); }
    if (String(url).includes('/ai/models?')) return Response.json({ items: [
      {provider_name: '测试供应商', reference: `model:${1}`, provider_id: 1, upstream_model: 'chat', revision: 1,  id: 1, alias: 'chat', enabled: true, media: { vision_enabled: false } },
      {provider_name: '测试供应商', reference: `model:${2}`, provider_id: 1, upstream_model: 'shared', revision: 1,  id: 2, alias: 'shared', enabled: true, media: { vision_enabled: true } },
    ], next_cursor: null });
    return Response.json({ items: String(url).includes('/agents?') ? [agent] : [], next_cursor: null });
  }));
  render(<Agents token="offline" />);
  await screen.findByLabelText('角色');
  await waitFor(() => expect(screen.getByRole('combobox', { name: '聊天模型' })).toBeEnabled());
  await waitFor(() => expect(screen.getByText(/聊天模型、图片理解模型和错误转发模型可以选择同一个模型/)).toBeVisible());
  expect(selectDisplay(screen.getByRole('combobox', { name: '图片理解模型' }))).toHaveTextContent('未配置：带图请求将报错');
  expect(screen.getByText(/带图请求必须配置图片理解模型/)).toBeVisible();
  for (const label of ['聊天模型', '图片理解模型', '错误转发模型']) {
    await chooseSelect(screen.getByRole('combobox', { name: label }), '测试供应商 / shared');
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
      {provider_name: '测试供应商', reference: `model:${1}`, provider_id: 1, upstream_model: 'chat', revision: 1,  id: 1, alias: 'chat', enabled: true, media: { vision_enabled: false } },
      {provider_name: '测试供应商', reference: `model:${2}`, provider_id: 1, upstream_model: 'shared', revision: 1,  id: 2, alias: 'shared', enabled: true, media: { vision_enabled: true } },
    ], next_cursor: null });
    return Response.json({ items: String(url).includes('/agents?') ? [agent] : [], next_cursor: null });
  }));
  render(<Agents token="offline" />);
  await screen.findByLabelText('角色');
  await waitFor(() => expect(screen.getByRole('combobox', { name: '聊天模型' })).toBeEnabled());
  for (const label of ['聊天模型', '图片理解模型', '错误转发模型']) {
    await chooseSelect(screen.getByRole('combobox', { name: label }), '测试供应商 / shared');
  }
  fireEvent.change(screen.getByLabelText('角色'), { target: { value: '保留用户编写的提示词' } });
  const save = screen.getByRole('button', { name: '保存 Agent' });
  save.focus();
  fireEvent.click(save);
  expect(await screen.findByText(message)).toBeVisible();
  // 保存错误获得焦点，浏览器会把长表单上方的错误滚入视口。
  expect(screen.getByRole('group', { name: '操作错误' })).toHaveFocus();
  for (const label of ['聊天模型', '图片理解模型', '错误转发模型']) {
    expect(selectDisplay(screen.getByRole('combobox', { name: label }))).toHaveTextContent('测试供应商 / shared');
  }
  expect(screen.getByLabelText('角色')).toHaveValue('保留用户编写的提示词');
  fireEvent.click(screen.getByRole('button', { name: '保存 Agent' }));
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
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
      {provider_name: '测试供应商', reference: `model:${1}`, provider_id: 1, upstream_model: 'chat', revision: 1,  id: 1, alias: 'chat', enabled: true, media: { vision_enabled: false } },
      {provider_name: '测试供应商', reference: `model:${2}`, provider_id: 1, upstream_model: 'vision', revision: 1,  id: 2, alias: 'vision', enabled: true, media: { vision_enabled: true } },
      {provider_name: '测试供应商', reference: `model:${3}`, provider_id: 1, upstream_model: 'backup', revision: 1,  id: 3, alias: 'backup', enabled: true, media: { vision_enabled: true } },
    ], next_cursor: null });
    return Response.json({ items: String(url).includes('/agents?') ? [agent] : [], next_cursor: null });
  }));
  render(<Agents token="offline" />);
  await screen.findByLabelText('角色');
  await waitFor(() => expect(screen.getByRole('combobox', { name: '聊天模型' })).toBeEnabled());
  await chooseSelect(screen.getByRole('combobox', { name: '图片理解模型' }), '测试供应商 / vision');
  await chooseSelect(screen.getByRole('combobox', { name: '错误转发模型' }), '测试供应商 / backup');
  fireEvent.click(screen.getByRole('button', { name: '保存 Agent' }));
  await waitFor(() => expect(patches).toEqual([
    { expected_revision: 1, vision_model_id: 2, fallback_model_id: 3 },
  ]));
  await waitFor(() => expect(screen.getByRole('button', { name: '保存 Agent' })).toBeEnabled());
  await waitFor(() => expect(screen.getByRole('combobox', { name: '图片理解模型' })).toBeEnabled());
  clearSelect(screen.getByRole('combobox', { name: '图片理解模型' }));
  clearSelect(screen.getByRole('combobox', { name: '错误转发模型' }));
  fireEvent.click(screen.getByRole('button', { name: '保存 Agent' }));
  await waitFor(() => expect(patches[1]).toEqual({
    expected_revision: 2, vision_model_id: null, fallback_model_id: null,
  }));
});

it('关联远程服务只保存服务 ID，无需把远端工具写入 Agent 工具配置', async () => {
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
  await screen.findByLabelText('角色');
  await waitFor(() => expect(screen.getByRole('combobox', { name: '关联远程服务（最多 3 个）' })).toBeEnabled());
  fireEvent.mouseDown(screen.getByRole('combobox', { name: '关联远程服务（最多 3 个）' }));
  fireEvent.click(await screen.findByText('文档服务 · docs'));
  expect(screen.getByText(/已关联远程服务的工具自动可用/)).toBeVisible();
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
  await screen.findByLabelText('角色');
  expect(selectDisplay(screen.getByRole('combobox', { name: '聊天模型' }))).toHaveTextContent(/90|测试供应商 \/ qwen/);
  fireEvent.change(screen.getByLabelText('角色'),{target:{value:'尚未保存的修改'}});
  fireEvent.click(screen.getByRole('button',{name:'保存 Agent'}));
  expect(await screen.findByText('配置版本冲突')).toBeInTheDocument();
  expect(screen.getByLabelText('角色')).toHaveValue('尚未保存的修改');
  await waitFor(()=>expect(JSON.parse(fetcher.mock.calls.find(([,init])=>init?.method==='PATCH')![1]!.body)).toEqual({expected_revision:1,system_prompt:'尚未保存的修改'}));
});

it('刷新时读取最新模型状态，不沿用页面首次访问的停用快照', async () => {
  let enabled = false;
  const item = { id: 1, revision: 1, code: 'helper', name: '助手', description: '', enabled: true,
    model_id: 9, vision_model_id: null, fallback_model_id: null, skill_ids: [], mcp_ids: [], tools: [], system_prompt: '', max_steps: 3, options: {} };
  vi.stubGlobal('fetch', vi.fn(async url => {
    if (String(url).includes('/ai/models?')) return Response.json({ items: [
      {provider_name: '测试供应商', reference: `model:${9}`,  id: 9, revision: 1, alias: 'local-model', upstream_model: 'qwen', provider_id: 1, enabled },
    ], next_cursor: null });
    return Response.json({ items: String(url).includes('/agents?') ? [item] : [], next_cursor: null });
  }));
  render(<Agents token="offline" />);
  await screen.findByLabelText('角色');
  await waitFor(() => expect(screen.getByRole('combobox', { name: '聊天模型' })).toBeEnabled());
  expect(within(screen.getByRole('combobox', { name: '聊天模型' })).queryByRole('option', { name: /local-model/ })).not.toBeInTheDocument();
  await waitFor(() => expect(screen.getByText('聊天模型已停用，请重新选择已启用的模型。')).toBeVisible());
  // 保留已有绑定，不因隐藏停用项而自动换成其他模型。
  expect(selectDisplay(screen.getByRole('combobox', { name: '聊天模型' }))).toHaveTextContent('测试供应商 / qwen');
  // 对应切换至供应商页面启用模型，再回到仍挂载着的 Agent 页面。
  enabled = true;
  fireEvent.click(screen.getByRole('button', { name: /^刷\s*新$/ }));
  await waitFor(() => expect(selectDisplay(screen.getByRole('combobox', { name: '聊天模型' }))).toHaveTextContent('测试供应商 / qwen'));
  await waitFor(() => expect(screen.queryByText('聊天模型已停用，请重新选择已启用的模型。')).not.toBeInTheDocument());
  expect(selectDisplay(screen.getByRole('combobox', { name: '聊天模型' }))).toHaveTextContent('测试供应商 / qwen');
  expect(within(screen.getByRole('combobox', { name: '聊天模型' })).queryByRole('option', { name: /local-model（停用）/ })).not.toBeInTheDocument();
});

it('模型翻页保留已选 ID，刷新失败明确报错而不把未知状态显示为停用', async () => {
  let failed = false;
  const item = { id: 1, revision: 1, code: 'helper', name: '助手', description: '', enabled: true,
    model_id: 90, vision_model_id: null, fallback_model_id: null, skill_ids: [], mcp_ids: [], tools: [], system_prompt: '', max_steps: 3, options: {} };
  vi.stubGlobal('fetch', vi.fn(async url => {
    if (String(url).includes('/ai/models?')) {
      if (failed) return Response.json({ message: '目录暂时不可用' }, { status: 503 });
      return Response.json(String(url).includes('cursor=1') ? { items: [
        {provider_name: '测试供应商', reference: `model:${90}`, revision: 1,  id: 90, alias: 'enabled-model', upstream_model: 'qwen', provider_id: 1, enabled: true },
      ], next_cursor: null } : { items: [
        {provider_name: '测试供应商', reference: `model:${1}`, revision: 1,  id: 1, alias: 'disabled-model', upstream_model: 'old', provider_id: 1, enabled: false },
      ], next_cursor: '1' });
    }
    return Response.json({ items: String(url).includes('/agents?') ? [item] : [], next_cursor: null });
  }));
  render(<Agents token="offline" />);
  await screen.findByLabelText('角色');
  fireEvent.change(screen.getByLabelText('角色'), { target: { value: '保留草稿' } });
  fireEvent.click(await screen.findByRole('button', { name: '加载更多模型' }));
  await waitFor(() => expect(selectDisplay(screen.getByRole('combobox', { name: '聊天模型' }))).toHaveTextContent('测试供应商 / qwen'));
  for (const label of ['聊天模型', '图片理解模型', '错误转发模型']) {
    expect(within(screen.getByRole('combobox', { name: label })).queryByRole('option', { name: /disabled-model/ })).not.toBeInTheDocument();
  }
  expect(selectDisplay(screen.getByRole('combobox', { name: '聊天模型' }))).toHaveTextContent(/90|测试供应商 \/ qwen/);
  failed = true;
  fireEvent.click(screen.getByRole('button', { name: '刷新模型状态' }));
  expect(await screen.findByText(/模型列表读取失败：目录暂时不可用/)).toBeVisible();
  expect(screen.queryByRole('option', { name: /停用/ })).not.toBeInTheDocument();
  expect(selectDisplay(screen.getByRole('combobox', { name: '聊天模型' }))).toHaveTextContent(/90|测试供应商 \/ qwen/);
  expect(screen.getByLabelText('角色')).toHaveValue('保留草稿');
});

it('刷新目录保留编辑基线，保存冲突重试不覆盖最新服务端版本', async () => {
  const original = { id: 1, revision: 4, code: 'helper', name: '助手', description: '', enabled: true,
    model_id: 1, vision_model_id: null, fallback_model_id: null, skill_ids: [80], mcp_ids: [90],
    tools: ['memory'], system_prompt: '原角色', personality_prompt: '原性格', options: {} };
  let item = original;
  const patches: unknown[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    if (init?.method === 'PATCH') {
      patches.push(JSON.parse(String(init.body)));
      return Response.json({ message: '配置版本冲突', code: 'AI_CONFIG_CONFLICT' }, { status: 409 });
    }
    return Response.json({ items: String(url).includes('/agents?') ? [item] : [], next_cursor: null });
  }));
  render(<Agents token="offline" />);
  await screen.findByLabelText('角色');
  fireEvent.change(screen.getByLabelText('角色'), { target: { value: '用户草稿' } });
  item = { ...original, revision: 9, name: '服务器助手', system_prompt: '外部已修改' };
  fireEvent.click(screen.getByRole('button', { name: /^刷\s*新$/ }));
  await screen.findByRole('button', { name: '服务器助手' });
  expect(screen.getByLabelText('角色')).toHaveValue('用户草稿');
  expect(screen.getByLabelText('性格')).toHaveValue('原性格');
  fireEvent.click(screen.getByRole('button', { name: '保存 Agent' }));
  await screen.findByText('配置版本冲突');
  expect(screen.getByRole('group', { name: '操作错误' })).toHaveFocus();
  fireEvent.click(screen.getByRole('button', { name: '保存 Agent' }));
  await waitFor(() => expect(patches).toHaveLength(2));
  expect(patches).toEqual([
    { expected_revision: 4, system_prompt: '用户草稿' },
    { expected_revision: 4, system_prompt: '用户草稿' },
  ]);
  expect(screen.getByLabelText('角色')).toHaveValue('用户草稿');
});

it('启用 Agent 禁止删除，停用 Agent 在页内确认后才删除', async () => {
  const base = { revision: 3, description: '', model_id: 1, vision_model_id: null,
    fallback_model_id: null, skill_ids: [], mcp_ids: [], tools: [], system_prompt: '', options: {} };
  const items = [
    { ...base, id: 1, name: '运行助手', code: 'running', enabled: true },
    { ...base, id: 2, name: '停用助手', code: 'stopped', enabled: false },
  ];
  const fetcher = vi.fn(async (url, init) => init?.method === 'DELETE'
    ? new Response(null, { status: 204 })
    : Response.json({ items: String(url).includes('/agents?') ? items : [], next_cursor: null }));
  vi.stubGlobal('fetch', fetcher);
  render(<Agents token="offline" />);
  await screen.findByLabelText('角色');
  expect(screen.getByRole('button', { name: '删除 Agent' })).toBeDisabled();
  const switches = screen.getByRole('group', { name: '切换 Agent' });
  fireEvent.click(within(switches).getByRole('button', { name: '停用助手' }));
  fireEvent.click(screen.getByRole('button', { name: '删除 Agent' }));
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: '确认删除' })).toBeEnabled();
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === 'DELETE')).toHaveLength(0);
  fireEvent.click(screen.getByRole('button', { name: '取消删除' }));
  expect(screen.queryByRole('button', { name: '确认删除' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '删除 Agent' }));
  fireEvent.click(screen.getByRole('button', { name: '确认删除' }));
  await waitFor(() => expect(within(switches).queryByRole('button', { name: '停用助手' })).not.toBeInTheDocument());
  const deleted = fetcher.mock.calls.find(([, init]) => init?.method === 'DELETE')!;
  expect(String(deleted[0])).toBe('/api/v1/agents/2');
  expect(JSON.parse(String(deleted[1]?.body))).toEqual({ expected_revision: 3 });
  expect(screen.getByLabelText('名称')).toHaveValue('运行助手');
});

it('分页后刷新仍显示当前 Agent，单独读取最新数据且保留已编辑草稿版本', async () => {
  const first = { id: 1, revision: 1, name: '首屏助手', code: 'first', description: '',
    enabled: false, model_id: 1, vision_model_id: null, fallback_model_id: null,
    system_prompt: '首屏角色', personality_prompt: '', tools: [], skill_ids: [], mcp_ids: [], options: {} };
  let second = { ...first, id: 2, revision: 6, name: '次页助手', code: 'second', system_prompt: '次页角色' };
  const patches: unknown[] = [];
  const fetcher = vi.fn(async (url, init) => {
    if (init?.method === 'PATCH') {
      const body = JSON.parse(String(init.body));
      patches.push(body);
      return Response.json({ ...second, ...body, revision: 9 });
    }
    if (String(url).endsWith('/agents/2')) return Response.json(second);
    if (String(url).includes('/agents?')) return Response.json(String(url).includes('cursor=next')
      ? { items: [second], next_cursor: null } : { items: [first], next_cursor: 'next' });
    return Response.json({ items: [], next_cursor: null });
  });
  vi.stubGlobal('fetch', fetcher);
  render(<Agents token="offline" />);
  await screen.findByLabelText('角色');
  fireEvent.click(screen.getByRole('button', { name: '加载更多 Agent' }));
  fireEvent.click(await screen.findByRole('button', { name: '次页助手' }));
  expect(screen.getByLabelText('角色')).toHaveValue('次页角色');
  second = { ...second, revision: 7, name: '次页已刷新', system_prompt: '次页服务端更新' };
  fireEvent.click(screen.getByRole('button', { name: /^刷\s*新$/ }));
  await screen.findByRole('button', { name: '次页已刷新' });
  expect(screen.getByLabelText('角色')).toHaveValue('次页服务端更新');
  expect(screen.getByRole('button', { name: '次页已刷新' })).toHaveAttribute('aria-pressed', 'true');
  fireEvent.change(screen.getByLabelText('角色'), { target: { value: '分页 Agent 草稿' } });
  second = { ...second, revision: 8, name: '次页再次刷新', system_prompt: '外部再次更新' };
  fireEvent.click(screen.getByRole('button', { name: /^刷\s*新$/ }));
  await screen.findByRole('button', { name: '次页再次刷新' });
  expect(screen.getByLabelText('角色')).toHaveValue('分页 Agent 草稿');
  fireEvent.click(screen.getByRole('button', { name: '保存 Agent' }));
  await waitFor(() => expect(patches).toEqual([
    { expected_revision: 7, system_prompt: '分页 Agent 草稿' },
  ]));
  expect(fetcher.mock.calls.filter(([url, init]) => String(url).endsWith('/agents/2')
    && init?.method === 'GET')).toHaveLength(2);
});
