import { useRunningPluginFixture } from '../test-plugins';
import { chooseSelect, selectDisplay } from '../test-select';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { Settings } from './Settings';

useRunningPluginFixture();

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
  ] };
const firstPage = { items: [
  {provider_name: '测试供应商', reference: `model:${1}`, provider_id: 1, upstream_model: 'universal', revision: 1,  id: 1, alias: 'universal', enabled: true },
  {provider_name: '测试供应商', reference: `model:${2}`, provider_id: 1, upstream_model: 'retired-private', revision: 1,  id: 2, alias: 'retired-private', enabled: false },
], next_cursor: 'second-page' };

it.each([{ name: '聊天与图片理解', selectedFields: modelFields.slice(0, 2) },
  { name: '图片生成与语音', selectedFields: modelFields.slice(2, 4) }])('$name 共享分页目录并按稳定引用保存，跨字段允许复用', async ({ selectedFields }) => {
  const modelReads: string[] = [];
  let submitted: unknown;
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    if (String(url).includes('/ai/models?')) {
      modelReads.push(String(url));
      return Response.json(String(url).includes('cursor=second-page')
        ? { items: [{provider_name: '测试供应商', reference: `model:${8}`, provider_id: 1, upstream_model: 'later-model', revision: 1,  id: 8, alias: 'later-model', enabled: true }], next_cursor: null }
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
  for (const [, label, value] of selectedFields) {
    const select = screen.getByLabelText(label);
    expect(selectDisplay(select)).toHaveTextContent(value === 'retired-private' ? '测试供应商 / retired-private（已停用）' : value);
  }
  expect(modelReads).toHaveLength(1);
  fireEvent.click(screen.getByText('加载更多模型'));
  await waitFor(() => expect(modelReads).toHaveLength(2));
  await waitFor(() => expect(screen.getByLabelText(selectedFields[0][1])).toBeEnabled());
  for (const [, label] of selectedFields) {
    await chooseSelect(screen.getByLabelText(label), '测试供应商 / later-model');
  }
  fireEvent.click(screen.getByText('保存修改'));
  await waitFor(() => expect(submitted).toEqual({ expected_revision: 4, values: Object.fromEntries(selectedFields.map(([name]) => [name, 'model:8'])) }));
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
  for (const [, label, value] of modelFields) expect(selectDisplay(screen.getByLabelText(label))).toHaveTextContent(value === 'retired-private' && reads > 1 ? '测试供应商 / retired-private（已停用）' : value);
  fireEvent.click(screen.getByRole('button', { name: '刷新模型列表' }));
  await waitFor(() => expect(screen.queryByText('模型列表读取失败：模型目录暂不可用')).not.toBeInTheDocument());
  expect(screen.queryByText('模型列表读取失败：模型目录暂不可用')).not.toBeInTheDocument();
  for (const [, label, value] of modelFields) expect(selectDisplay(screen.getByLabelText(label))).toHaveTextContent(value === 'retired-private' && reads > 1 ? '测试供应商 / retired-private（已停用）' : value);
  expect(screen.getByLabelText('系统提示词')).toHaveValue('未保存提示词');
  expect(screen.getAllByText(/当前配置的模型未启用或不存在，请重新选择/)).toHaveLength(4);
  expect(screen.getByText('测试供应商 / retired-private（已停用）')).toBeVisible();
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
      return Response.json({ items: [{provider_name: '测试供应商', reference: `model:${6}`, provider_id: 1, upstream_model: 'new-token-model', revision: 1,  id: 6, alias: 'new-token-model', enabled: true }], next_cursor: null });
    }
    return Response.json(String(url).endsWith('/settings') ? snapshot
      : { available: false, text: 0, images: 0, total: 0 });
  }));
  const { rerender } = render(<Settings token="old-token" />);
  fireEvent.click(screen.getByRole('button', { name: 'LLM设置' }));
  await waitFor(() => expect(oldSignal).toBeDefined());
  rerender(<Settings token="new-token" />);
  await waitFor(() => expect(screen.getByLabelText('聊天模型')).toBeEnabled());
  await chooseSelect(screen.getByLabelText('聊天模型'), '测试供应商 / new-token-model');
  expect(oldSignal?.aborted).toBe(true);
  await act(async () => { finishOld(Response.json(firstPage)); });
  expect(selectDisplay(screen.getByLabelText('聊天模型'))).toHaveTextContent('测试供应商 / new-token-model');
  expect(screen.queryByRole('option', { name: 'universal' })).not.toBeInTheDocument();
});
