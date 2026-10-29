import { chooseSelect, selectDisplay } from '../test-select';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { AiApi } from '../ai-api';
import { Models } from './Models';
import type { AiModel, Provider } from '../ai-types';

const provider: Provider = { id: 1, revision: 1, name: '本机', enabled: true,
  protocol: 'openai-chat', base_url: 'https://offline.invalid/v1', description: '',
  credential_set: false, credential_available: true };

// 媒体开关测试仅验证本地配置；专门的同步测试恢复真实接口调用。
beforeEach(() => {
  vi.spyOn(AiApi.prototype, 'upstreamModels').mockResolvedValue({ provider_id: 1, revision: 1, models: [] });
});

it('配置直接生图协议时保留识图和音频能力，使用当前版本保存', async () => {
  let model: AiModel = {provider_name: '测试供应商', reference: `model:${1}`,  id: 1, revision: 3, alias: 'image', upstream_model: 'upstream-image',
    provider_id: 1, enabled: true, media_initialized: true,
    media: { vision_enabled: true, image_api_profile: 'none', audio_chat_enabled: true } };
  const patches: unknown[] = [];
  vi.stubGlobal('fetch', vi.fn(async (_url, init) => {
    if (init?.method === 'PATCH') {
      const patch = JSON.parse(String(init.body)); patches.push(patch);
      model = { ...model, media: patch.media, revision: model.revision + 1 };
      return Response.json(model);
    }
    return Response.json({ items: [model], next_cursor: null });
  }));
  render(<Models token="offline" provider={provider} />);
  // 上游模型名称不能用来猜测接口协议，管理员显式声明图片生成能力。
  await chooseSelect(await screen.findByRole('combobox', { name: '图片生成协议 upstream-image' }), 'GPT Image');
  await waitFor(() => expect(selectDisplay(screen.getByRole('combobox', { name: '图片生成协议 upstream-image' })))
    .toHaveTextContent('GPT Image'));
  expect(patches).toEqual([{ expected_revision: 3,
    media: { vision_enabled: true, image_api_profile: 'gpt-image', audio_chat_enabled: true } }]);
});

it('开启或关闭图片理解时保存当前版本并保留图片生成、音频及模型状态', async () => {
  let model: AiModel = {provider_name: '测试供应商', reference: `model:${1}`,  id: 1, revision: 3, alias: 'vision', upstream_model: 'upstream-vision',
    provider_id: 1, enabled: true, media_initialized: true,
    media: { vision_enabled: false, image_api_profile: 'gpt-image', audio_chat_enabled: true } };
  const patches: unknown[] = [];
  vi.stubGlobal('fetch', vi.fn(async (_url, init) => {
    if (init?.method === 'PATCH') {
      const patch = JSON.parse(String(init.body)); patches.push(patch);
      model = { ...model, media: patch.media, revision: model.revision + 1 };
      return Response.json(model);
    }
    return Response.json({ items: [model], next_cursor: null });
  }));
  render(<Models token="offline" provider={provider} />);
  fireEvent.click(await screen.findByRole('switch', { name: '图片理解 upstream-vision' }));
  await waitFor(() => expect(screen.getByRole('switch', { name: '图片理解 upstream-vision' })).toBeChecked());
  fireEvent.click(screen.getByRole('switch', { name: '图片理解 upstream-vision' }));
  await waitFor(() => expect(screen.getByRole('switch', { name: '图片理解 upstream-vision' })).not.toBeChecked());
  expect(patches).toEqual([
    { expected_revision: 3, media: { vision_enabled: true, image_api_profile: 'gpt-image', audio_chat_enabled: true } },
    { expected_revision: 4, media: { vision_enabled: false, image_api_profile: 'gpt-image', audio_chat_enabled: true } },
  ]);
  expect(screen.getByRole('switch', { name: '启用 upstream-vision' })).toBeChecked();
  expect(screen.getByText('upstream-vision')).toBeVisible();
});

it('图片理解保存冲突后显示数据库状态，再次操作使用新版本', async () => {
  let model: AiModel = {provider_name: '测试供应商', reference: `model:${1}`,  id: 1, revision: 3, alias: 'vision', upstream_model: 'model',
    provider_id: 1, enabled: true, media_initialized: true,
    media: { vision_enabled: false, image_api_profile: 'none', audio_chat_enabled: false } };
  const patches: { expected_revision: number }[] = [];
  vi.stubGlobal('fetch', vi.fn(async (_url, init) => {
    if (init?.method === 'PATCH') {
      const patch = JSON.parse(String(init.body)); patches.push(patch);
      if (patch.expected_revision === 3) {
        model = { ...model, revision: 4 };
        return Response.json({ message: '版本冲突' }, { status: 409 });
      }
      model = { ...model, media: patch.media, revision: 5 };
      return Response.json(model);
    }
    return Response.json({ items: [model], next_cursor: null });
  }));
  render(<Models token="offline" provider={provider} />);
  fireEvent.click(await screen.findByRole('switch', { name: '图片理解 model' }));
  expect(await screen.findByText(/版本冲突/)).toBeVisible();
  expect(screen.getByRole('switch', { name: '图片理解 model' })).not.toBeChecked();
  fireEvent.click(screen.getByRole('switch', { name: '图片理解 model' }));
  await waitFor(() => expect(screen.getByRole('switch', { name: '图片理解 model' })).toBeChecked());
  expect(patches.map(patch => patch.expected_revision)).toEqual([3, 4]);
});

it('切换模型启用状态时提交当前版本且不覆盖媒体能力', async () => {
  let model: AiModel = {provider_name: '测试供应商', reference: `model:${1}`,  id: 1, revision: 3, alias: 'local-qwen', upstream_model: 'Qwen/Qwen3.5-9B',
    provider_id: 1, enabled: false, media_initialized: true,
    media: { vision_enabled: true, image_api_profile: 'none', audio_chat_enabled: false } };
  const patches: unknown[] = [];
  vi.stubGlobal('fetch', vi.fn(async (_url, init) => {
    if (init?.method === 'PATCH') {
      const patch = JSON.parse(String(init.body)); patches.push(patch);
      model = { ...model, enabled: patch.enabled, revision: model.revision + 1 };
      return Response.json(model);
    }
    return Response.json({ items: [model], next_cursor: null });
  }));
  render(<Models token="offline" provider={provider} />);
  fireEvent.click(await screen.findByRole('switch', { name: '启用 Qwen/Qwen3.5-9B' }));
  await waitFor(() => expect(screen.getByRole('switch', { name: '启用 Qwen/Qwen3.5-9B' })).toBeChecked());
  fireEvent.click(screen.getByRole('switch', { name: '启用 Qwen/Qwen3.5-9B' }));
  await waitFor(() => expect(screen.getByRole('switch', { name: '启用 Qwen/Qwen3.5-9B' })).not.toBeChecked());
  expect(patches).toEqual([{ expected_revision: 3, enabled: true }, { expected_revision: 4, enabled: false }]);
  expect(model.media?.vision_enabled).toBe(true);
  expect(screen.queryByRole('button', { name: '新增模型' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /编辑|删除|导入/ })).not.toBeInTheDocument();
});

it('切换失败保留真实状态，版本冲突可重新读取后再操作', async () => {
  let revision = 3;
  vi.stubGlobal('fetch', vi.fn(async (_url, init) => {
    if (init?.method === 'PATCH') {
      if (JSON.parse(String(init.body)).expected_revision === 3) {
        revision = 4;
        return Response.json({ message: '版本冲突', code: 'CONFLICT' }, { status: 409 });
      }
      return Response.json({provider_name: '测试供应商', reference: `model:${1}`,  id: 1, revision: 5, alias: 'vision', upstream_model: 'model', provider_id: 1, enabled: false });
    }
    return Response.json({ items: [{provider_name: '测试供应商', reference: `model:${1}`, id: 1, revision, alias: 'vision', upstream_model: 'model', provider_id: 1, enabled: true }], next_cursor: null });
  }));
  render(<Models token="offline" provider={provider} />);
  fireEvent.click(await screen.findByRole('switch', { name: '启用 model' }));
  expect(await screen.findByText(/版本冲突/)).toBeVisible();
  expect(screen.getByRole('switch', { name: '启用 model' })).toBeChecked();
  // 冲突后重新加载已保存状态及版本，下一次操作能成功。
  fireEvent.click(screen.getByRole('switch', { name: '启用 model' }));
  await waitFor(() => expect(screen.getByRole('switch', { name: '启用 model' })).not.toBeChecked());
});

it('选择供应商自动同步全部模型，失败后保留已保存列表并显示错误', async () => {
  vi.mocked(AiApi.prototype.upstreamModels).mockRestore();
  vi.stubGlobal('fetch', vi.fn(async url => {
    if (String(url).includes('upstream-models')) return Response.json({ message: '上游服务不可用' }, { status: 502 });
    return Response.json({ items: [{provider_name: '测试供应商', reference: `model:${1}`,  id: 1, revision: 1, alias: 'existing', upstream_model: 'model', provider_id: 1, enabled: true }], next_cursor: null });
  }));
  render(<Models token="offline" provider={provider} />);
  await screen.findByText('model');
  expect(await screen.findByText(/上游服务不可用/)).toBeVisible();
  expect(screen.getByRole('switch', { name: '启用 model' })).toBeChecked();
});

it('同步同名上游模型无需别名，目录只呈现真实身份', async () => {
  vi.mocked(AiApi.prototype.upstreamModels).mockRestore();
  const models: AiModel[] = [];
  const imports: unknown[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    if (String(url).includes('upstream-models')) return Response.json({ provider_id: 1, revision: 1, models: ['shared', 'other'] });
    if (String(url).includes('models/import')) {
      const body = JSON.parse(String(init.body)); imports.push(body);
      models.push(...body.models.map((item: { upstream_model: string }, index: number) => ({ ...item,
        id: index + 1, revision: 1, provider_id: 1, provider_name: '本机', reference: `model:${index + 1}`, enabled: false, alias: null })));
      return Response.json({ imported: models, skipped: [], conflicts: [] });
    }
    return Response.json({ items: models, next_cursor: null });
  }));
  render(<Models token="offline" provider={provider} />);
  expect(await screen.findByRole('switch', { name: '启用 shared' })).not.toBeChecked();
  expect(screen.getByRole('switch', { name: '启用 other' })).not.toBeChecked();
  expect(screen.queryByText('模型别名')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /别名|导入/ })).not.toBeInTheDocument();
  expect(imports).toEqual([{ expected_revision: 1, models: [{ upstream_model: 'shared' }, { upstream_model: 'other' }] }]);
});
