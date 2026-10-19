import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { Settings } from './Settings';

const modelFields = [
  ['chat_model', '聊天模型', 'old-chat'],
  ['chat_vision_model', '图片理解模型', 'retired-private'],
  ['chat_image_model', '图片生成模型', 'unknown-image'],
  ['chat_tts_model', '语音模型', 'old-voice'],
];
const snapshot = { database_revision: 4, applied_revision: 4, startup_revision: null,
  restart_required_fields: [], fields: [
    ...modelFields.map(([name, description, value]) => ({ name, description, value, kind: 'string',
      nullable: name !== 'chat_model', secret_is_set: false, restart_required: false })),
    { name: 'chat_system_prompt', description: '系统提示词', value: '原提示', kind: 'string',
      nullable: false, secret_is_set: false, restart_required: false },
    { name: 'chat_max_sessions', description: '最大会话数', value: 20, kind: 'number',
      nullable: false, secret_is_set: false, restart_required: false },
    { name: 'chat_python_search_mcp_code', description: '搜索 MCP 服务', value: 'search', kind: 'string',
      nullable: true, secret_is_set: false, restart_required: false },
  ] };
const firstPage = { items: [
  { id: 1, alias: 'universal', enabled: true },
  { id: 2, alias: 'retired-private', enabled: false },
], next_cursor: 'second-page' };

it('隐藏旧搜索绑定字段，四个模型共享分页下拉并按别名保存，跨字段允许复用', async () => {
  const modelReads: string[] = [];
  let submitted: unknown;
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    if (String(url).includes('/ai/models?')) {
      modelReads.push(String(url));
      return Response.json(String(url).includes('cursor=second-page')
        ? { items: [{ id: 8, alias: 'later-model', enabled: true }], next_cursor: null }
        : firstPage);
    }
    if (init?.method === 'PATCH') submitted = JSON.parse(String(init.body));
    return Response.json(String(url).endsWith('/settings') ? snapshot
      : { available: false, text: 0, images: 0, total: 0 });
  }));
  render(<Settings token="offline" />);
  fireEvent.click(screen.getByRole('button', { name: 'LLM设置' }));
  await screen.findByLabelText('系统提示词');
  expect(screen.queryByLabelText('搜索 MCP 服务')).not.toBeInTheDocument();
  expect(screen.queryByText('chat_python_search_mcp_code')).not.toBeInTheDocument();
  for (const [, label, value] of modelFields) {
    const select = screen.getByRole('combobox', { name: label });
    expect(select).toHaveValue(value);
    expect(await within(select).findByRole('option', { name: 'universal' })).toBeInTheDocument();
    expect(within(select).queryByRole('option', { name: /retired-private/ })).not.toBeInTheDocument();
  }
  expect(modelReads).toHaveLength(1);
  fireEvent.click(screen.getByRole('button', { name: '加载更多模型' }));
  await within(screen.getByLabelText('聊天模型')).findByRole('option', { name: 'later-model' });
  expect(modelReads).toHaveLength(2);
  for (const [, label] of modelFields) {
    fireEvent.change(screen.getByLabelText(label), { target: { value: 'later-model' } });
  }
  fireEvent.change(screen.getByLabelText('系统提示词'), { target: { value: '保留提示词草稿' } });
  fireEvent.click(screen.getByRole('button', { name: '基础设置' }));
  fireEvent.change(screen.getByLabelText('最大会话数'), { target: { value: '30' } });
  fireEvent.click(screen.getByRole('button', { name: 'LLM设置' }));
  for (const [, label] of modelFields) expect(screen.getByLabelText(label)).toHaveValue('later-model');
  expect(screen.getByLabelText('系统提示词')).toHaveValue('保留提示词草稿');
  fireEvent.click(screen.getByRole('button', { name: '保存修改' }));
  await waitFor(() => expect(submitted).toEqual({ expected_revision: 4, values: {
    chat_model: 'later-model', chat_vision_model: 'later-model', chat_image_model: 'later-model',
    chat_tts_model: 'later-model', chat_system_prompt: '保留提示词草稿', chat_max_sessions: 30,
  } }));
});

it('目录读取失败明确报错，保留原别名和草稿，刷新恢复后不自动替换模型', async () => {
  let reads = 0;
  vi.stubGlobal('fetch', vi.fn(async url => {
    if (String(url).includes('/ai/models?')) {
      reads += 1;
      return reads === 1
        ? Response.json({ code: 'UNAVAILABLE', message: '模型目录暂不可用' }, { status: 503 })
        : Response.json({ ...firstPage, next_cursor: null });
    }
    return Response.json(String(url).endsWith('/settings') ? snapshot
      : { available: false, text: 0, images: 0, total: 0 });
  }));
  render(<Settings token="offline" />);
  fireEvent.click(screen.getByRole('button', { name: 'LLM设置' }));
  fireEvent.change(await screen.findByLabelText('系统提示词'), { target: { value: '未保存提示词' } });
  expect(await screen.findByText('模型列表读取失败：模型目录暂不可用')).toBeVisible();
  for (const [, label, value] of modelFields) expect(screen.getByLabelText(label)).toHaveValue(value);
  fireEvent.click(screen.getByRole('button', { name: '刷新模型列表' }));
  await within(screen.getByLabelText('聊天模型')).findByRole('option', { name: 'universal' });
  expect(screen.queryByText('模型列表读取失败：模型目录暂不可用')).not.toBeInTheDocument();
  for (const [, label, value] of modelFields) expect(screen.getByLabelText(label)).toHaveValue(value);
  expect(screen.getByLabelText('系统提示词')).toHaveValue('未保存提示词');
  expect(screen.getAllByText(/当前配置的模型未启用或不存在，请重新选择/)).toHaveLength(4);
  expect(screen.queryByText('retired-private')).not.toBeInTheDocument();
});

it('切换凭据取消旧目录读取，迟到结果不覆盖新凭据目录', async () => {
  let finishOld!: (response: Response) => void;
  let oldSignal: AbortSignal | undefined;
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    if (String(url).includes('/ai/models?')) {
      if (new Headers(init?.headers).get('Authorization') === 'Bearer old-token') {
        oldSignal = init?.signal;
        return new Promise<Response>(resolve => { finishOld = resolve; });
      }
      return Response.json({ items: [{ id: 6, alias: 'new-token-model', enabled: true }], next_cursor: null });
    }
    return Response.json(String(url).endsWith('/settings') ? snapshot
      : { available: false, text: 0, images: 0, total: 0 });
  }));
  const { rerender } = render(<Settings token="old-token" />);
  fireEvent.click(screen.getByRole('button', { name: 'LLM设置' }));
  await waitFor(() => expect(oldSignal).toBeDefined());
  rerender(<Settings token="new-token" />);
  await within(await screen.findByLabelText('聊天模型')).findByRole('option', { name: 'new-token-model' });
  expect(oldSignal?.aborted).toBe(true);
  await act(async () => { finishOld(Response.json(firstPage)); });
  expect(within(screen.getByLabelText('聊天模型')).getByRole('option', { name: 'new-token-model' })).toBeInTheDocument();
  expect(screen.queryByRole('option', { name: 'universal' })).not.toBeInTheDocument();
});
