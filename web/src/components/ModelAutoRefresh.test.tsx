import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { AgentModelSelect } from './AgentModelSelect';
import { MediaGenerator } from './MediaGenerator';
import { Chat } from '../pages/Chat';
import { Settings } from '../pages/Settings';

it.each(['Agent', '设置', '对话', '媒体'] as const)('%s 每次打开模型选择框读取新目录，保留当前绑定', async page => {
  let reads = 0, name = '初始模型';
  const changed = vi.fn();
  vi.stubGlobal('fetch', vi.fn(async url => {
    const path = String(url);
    if (path.includes('/ai/models?')) {
      reads += 1;
      return Response.json({ items: [{ id: 1, revision: reads, reference: 'model:1', alias: null,
        provider_id: 1, provider_name: '供应商', upstream_model: name, enabled: true,
        media_initialized: true, media: { vision_enabled: true, image_api_profile: 'gpt-image', audio_chat_enabled: true } }], next_cursor: null });
    }
    if (path.endsWith('/settings')) return Response.json({ database_revision: 1, applied_revision: 1,
      restart_required_fields: [], fields: [{ name: 'chat_model', description: '聊天模型', kind: 'string',
        value: 'model:1', nullable: false, secret_is_set: false, restart_required: false }] });
    if (path.endsWith('/status')) return Response.json({ configured_model: 'model:1', python_media: { storage_available: true } });
    return Response.json({ items: [], next_cursor: null });
  }));
  const label = page === 'Agent' ? '默认模型' : page === '设置' ? '聊天模型' : page === '对话' ? '测试模型' : '生成模型';
  render(page === 'Agent' ? <AgentModelSelect token="offline" value={1} onChange={changed} />
    : page === '设置' ? <Settings token="offline" embedded categoryKeys={['llm']} />
    : page === '对话' ? <Chat token="offline" /> : <MediaGenerator token="offline" />);
  await waitFor(() => expect(reads).toBe(1));
  const input = await screen.findByRole('combobox', { name: label });
  await waitFor(() => expect(input).toBeEnabled());
  expect(screen.queryByRole('button', { name: /刷新模型/ })).not.toBeInTheDocument();
  for (const next of ['更新模型', '再次更新']) {
    name = next;
    const before = reads;
    fireEvent.mouseDown(input);
    await waitFor(() => expect(reads).toBe(before + 1));
    await waitFor(() => {
      const options = document.getElementById(input.getAttribute('aria-controls') ?? '');
      expect(options).not.toBeNull();
      expect(within(options!).getByRole('option', { name: `${next}` })).toBeVisible();
    });
    fireEvent.keyDown(input, { key: 'Escape', code: 'Escape', keyCode: 27 });
    await waitFor(() => expect(input).toHaveAttribute('aria-expanded', 'false'));
  }
  expect(changed).not.toHaveBeenCalled();
});

it('重新打开会取消慢目录请求，迟到结果不能覆盖新候选', async () => {
  let reads = 0, staleSignal: AbortSignal | undefined;
  let finishStale!: (response: Response) => void;
  /** 各次目录使用同一模型身份，区分候选刷新与用户修改绑定。 */
  const page = (name: string) => ({ items: [{ id: 1, revision: 1, reference: 'model:1', provider_id: 1,
    provider_name: '供应商', upstream_model: name, enabled: true }], next_cursor: null });
  vi.stubGlobal('fetch', vi.fn(async (_url, init) => {
    reads += 1;
    if (reads === 2) {
      staleSignal = init?.signal;
      return new Promise<Response>(resolve => { finishStale = resolve; });
    }
    return Response.json(page(reads === 1 ? '原模型' : '最新模型'));
  }));
  const changed = vi.fn();
  render(<AgentModelSelect token="offline" value={1} onChange={changed} />);
  await screen.findByText('原模型');
  const input = screen.getByRole('combobox', { name: '默认模型' });
  fireEvent.mouseDown(input);
  await waitFor(() => expect(staleSignal).toBeDefined());
  fireEvent.click(screen.getByRole('option', { name: '原模型' }));
  expect(changed).not.toHaveBeenCalled();
  fireEvent.keyDown(input, { key: 'Escape', keyCode: 27 });
  fireEvent.mouseDown(input);
  await waitFor(() => expect(staleSignal?.aborted).toBe(true));
  await waitFor(() => expect(screen.getByRole('option', { name: '最新模型' })).toBeVisible());
  await act(async () => { finishStale(Response.json(page('迟到模型'))); });
  expect(screen.queryByText('迟到模型')).not.toBeInTheDocument();
  expect(changed).not.toHaveBeenCalled();
});
