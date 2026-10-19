import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { Models } from './Models';
import type { AiModel, Provider } from '../ai-types';

const provider: Provider = { id: 1, revision: 1, name: '本机', enabled: true,
  protocol: 'openai-chat', base_url: 'https://offline.invalid/v1', description: '',
  credential_set: false, credential_available: true };

it('配置直接生图协议时保留识图和音频能力，使用当前版本保存', async () => {
  let model: AiModel = { id: 1, revision: 3, alias: 'image', upstream_model: 'upstream-image',
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
  fireEvent.change(await screen.findByRole('combobox', { name: '图片生成协议 image' }),
    { target: { value: 'gpt-image' } });
  await waitFor(() => expect(screen.getByRole('combobox', { name: '图片生成协议 image' }))
    .toHaveValue('gpt-image'));
  expect(patches).toEqual([{ expected_revision: 3,
    media: { vision_enabled: true, image_api_profile: 'gpt-image', audio_chat_enabled: true } }]);
});

it('开启或关闭图片理解时保存当前版本并保留图片生成、音频及模型状态', async () => {
  let model: AiModel = { id: 1, revision: 3, alias: 'vision', upstream_model: 'upstream-vision',
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
  fireEvent.click(await screen.findByRole('switch', { name: '图片理解 vision' }));
  await waitFor(() => expect(screen.getByRole('switch', { name: '图片理解 vision' })).toBeChecked());
  fireEvent.click(screen.getByRole('switch', { name: '图片理解 vision' }));
  await waitFor(() => expect(screen.getByRole('switch', { name: '图片理解 vision' })).not.toBeChecked());
  expect(patches).toEqual([
    { expected_revision: 3, media: { vision_enabled: true, image_api_profile: 'gpt-image', audio_chat_enabled: true } },
    { expected_revision: 4, media: { vision_enabled: false, image_api_profile: 'gpt-image', audio_chat_enabled: true } },
  ]);
  expect(screen.getByRole('switch', { name: '启用 vision' })).toBeChecked();
  expect(screen.getByText('upstream-vision')).toBeVisible();
});

it('图片理解保存冲突后显示数据库状态，再次操作使用新版本', async () => {
  let model: AiModel = { id: 1, revision: 3, alias: 'vision', upstream_model: 'model',
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
  fireEvent.click(await screen.findByRole('switch', { name: '图片理解 vision' }));
  expect(await screen.findByText(/版本冲突/)).toBeVisible();
  expect(screen.getByRole('switch', { name: '图片理解 vision' })).not.toBeChecked();
  fireEvent.click(screen.getByRole('switch', { name: '图片理解 vision' }));
  await waitFor(() => expect(screen.getByRole('switch', { name: '图片理解 vision' })).toBeChecked());
  expect(patches.map(patch => patch.expected_revision)).toEqual([3, 4]);
});

it('切换模型启用状态时提交当前版本且不覆盖媒体能力', async () => {
  let model: AiModel = { id: 1, revision: 3, alias: 'local-qwen', upstream_model: 'Qwen/Qwen3.5-9B',
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
  fireEvent.click(await screen.findByRole('switch', { name: '启用 local-qwen' }));
  await waitFor(() => expect(screen.getByRole('switch', { name: '启用 local-qwen' })).toBeChecked());
  fireEvent.click(screen.getByRole('switch', { name: '启用 local-qwen' }));
  await waitFor(() => expect(screen.getByRole('switch', { name: '启用 local-qwen' })).not.toBeChecked());
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
      return Response.json({ id: 1, revision: 5, alias: 'vision', upstream_model: 'model', provider_id: 1, enabled: false });
    }
    return Response.json({ items: [{ id: 1, revision, alias: 'vision', upstream_model: 'model', provider_id: 1, enabled: true }], next_cursor: null });
  }));
  render(<Models token="offline" provider={provider} />);
  fireEvent.click(await screen.findByRole('switch', { name: '启用 vision' }));
  expect(await screen.findByText(/版本冲突/)).toBeVisible();
  expect(screen.getByRole('switch', { name: '启用 vision' })).toBeChecked();
  // 冲突后重新加载已保存状态及版本，下一次操作能成功。
  fireEvent.click(screen.getByRole('switch', { name: '启用 vision' }));
  await waitFor(() => expect(screen.getByRole('switch', { name: '启用 vision' })).not.toBeChecked());
});

it('刷新获取全部模型，失败后保留已保存列表并显示错误', async () => {
  vi.stubGlobal('fetch', vi.fn(async url => {
    if (String(url).includes('upstream-models')) return Response.json({ message: '上游服务不可用' }, { status: 502 });
    return Response.json({ items: [{ id: 1, revision: 1, alias: 'existing', upstream_model: 'model', provider_id: 1, enabled: true }], next_cursor: null });
  }));
  render(<Models token="offline" provider={provider} />);
  await screen.findByText('existing');
  fireEvent.click(screen.getByRole('button', { name: '刷新模型' }));
  expect(await screen.findByText(/上游服务不可用/)).toBeVisible();
  expect(screen.getByRole('switch', { name: '启用 existing' })).toBeChecked();
});

it('同步保留已保存模型，显示别名冲突并用管理员输入的新别名导入', async () => {
  const models: AiModel[] = [];
  const imports: unknown[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    if (String(url).includes('upstream-models')) {
      return Response.json({ provider_id: 1, revision: 1, models: ['shared', 'other'] });
    }
    if (String(url).includes('models/import')) {
      const body = JSON.parse(String(init.body)); imports.push(body);
      if (body.skip_alias_conflicts) {
        models.push({ id: 2, revision: 1, alias: 'other', upstream_model: 'other', provider_id: 1, enabled: false });
        return Response.json({ provider_id: 1, revision: 1, imported: models, skipped: [],
          conflicts: [{ alias: 'shared', upstream_model: 'shared' }] });
      }
      if (body.models[0].alias === 'taken') return Response.json({ code: 'AI_MODEL_ALIAS_CONFLICT', message: '模型别名已被其他记录使用' }, { status: 409 });
      const imported = { ...body.models[0], id: 3, revision: 1, provider_id: 1, enabled: false };
      models.push(imported);
      return Response.json({ provider_id: 1, revision: 1, imported: [imported], skipped: [], conflicts: [] });
    }
    return Response.json({ items: models, next_cursor: null });
  }));
  render(<Models token="offline" provider={provider} />);
  await waitFor(() => expect(screen.getByRole('button', { name: '刷新模型' })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: '刷新模型' }));
  const other = await screen.findByRole('switch', { name: '启用 other' });
  expect(other).not.toBeChecked();
  fireEvent.click(await screen.findByRole('button', { name: '设置 shared 的别名并导入' }));
  fireEvent.change(screen.getByLabelText('模型别名'), { target: { value: 'taken' } });
  fireEvent.click(screen.getByRole('button', { name: '保存并导入' }));
  await waitFor(() => expect(screen.getByText('模型别名已被其他记录使用')).toBeVisible());
  expect(screen.getByLabelText('模型别名')).toHaveValue('taken');
  expect(other).not.toBeChecked();
  fireEvent.change(screen.getByLabelText('模型别名'), { target: { value: 'second-shared' } });
  fireEvent.click(screen.getByRole('button', { name: '保存并导入' }));
  expect(await screen.findByRole('switch', { name: '启用 second-shared' })).not.toBeChecked();
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '设置 shared 的别名并导入' })).not.toBeInTheDocument();
  expect(imports).toEqual([
    { expected_revision: 1, skip_alias_conflicts: true,
      models: [{ alias: 'shared', upstream_model: 'shared' }, { alias: 'other', upstream_model: 'other' }] },
    { expected_revision: 1, models: [{ alias: 'taken', upstream_model: 'shared' }] },
    { expected_revision: 1, models: [{ alias: 'second-shared', upstream_model: 'shared' }] },
  ]);
});

it('已有模型可去掉别名前缀，提交记录 ID 和版本而不改变上游名称或启用状态', async () => {
  const model = { id: 42, revision: 3, alias: '1/glm', upstream_model: 'glm', provider_id: 1, enabled: true };
  const writes: unknown[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    if (init?.method === 'PATCH') {
      expect(String(url)).toMatch(/ai\/models\/42$/);
      writes.push(JSON.parse(String(init.body)));
      return Response.json({ ...model, alias: 'glm', revision: 4 });
    }
    return Response.json({ items: [model], next_cursor: null });
  }));
  render(<Models token="offline" provider={provider} />);
  fireEvent.click(await screen.findByRole('button', { name: '修改 1/glm 别名' }));
  fireEvent.change(screen.getByLabelText('模型别名'), { target: { value: 'glm' } });
  fireEvent.click(screen.getByRole('button', { name: '保存别名' }));
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  expect(writes).toEqual([{ expected_revision: 3, alias: 'glm' }]);
  expect(screen.getByRole('switch', { name: '启用 glm' })).toBeChecked();
});

it('别名冲突时保留修改草稿与原模型状态', async () => {
  const model = { id: 42, revision: 3, alias: '1/glm', upstream_model: 'glm', provider_id: 1, enabled: true };
  vi.stubGlobal('fetch', vi.fn(async (_url, init) => {
    if (init?.method === 'PATCH') {
      return Response.json({ message: '模型别名已存在', code: 'CONFLICT' }, { status: 409 });
    }
    return Response.json({ items: [model], next_cursor: null });
  }));
  render(<Models token="offline" provider={provider} />);
  fireEvent.click(await screen.findByRole('button', { name: '修改 1/glm 别名' }));
  fireEvent.change(screen.getByLabelText('模型别名'), { target: { value: 'glm' } });
  fireEvent.click(screen.getByRole('button', { name: '保存别名' }));
  await waitFor(() => expect(screen.getByText(/模型别名已存在/)).toBeVisible());
  expect(screen.getByLabelText('模型别名')).toHaveValue('glm');
  expect(screen.getByRole('switch', { name: '启用 1/glm' })).toBeChecked();
});
