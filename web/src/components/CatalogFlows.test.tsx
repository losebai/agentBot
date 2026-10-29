import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { Providers } from '../pages/Providers';
import { Agents } from '../pages/Agents';
import { Skills } from '../pages/Skills';
import { McpServers } from '../pages/McpServers';
import type { AiModel, Provider } from '../ai-types';

const providers: Provider[] = [1, 2].map(id => ({ id, revision: 1, name: `供应商${id}`,
  protocol: 'openai-chat', base_url: `https://provider${id}.invalid/v1`, enabled: true,
  description: '', credential_set: false, credential_available: true }));

/** 离线目录按供应商过滤，捕获写入归属错误而不连接真实模型服务。 */
function catalogApi() {
  const models: AiModel[] = [1, 2].map(id => ({ id, provider_name: `供应商${id}`, reference: `model:${id}`, revision: 1, alias: `local-${id}`,
    upstream_model: `upstream-${id}`, provider_id: id, enabled: false }));
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    const target = new URL(String(url), 'http://offline.test');
    if (target.pathname.endsWith('/status')) return Response.json({ python_media: { initialized: false } });
    if (target.pathname.endsWith('/upstream-models')) return Response.json({ provider_id: 1, revision: 1, models: [] });
    if (target.pathname.endsWith('/ai/providers')) return Response.json({ items: providers, next_cursor: null });
    if (target.pathname.endsWith('/ai/models')) {
      if (init?.method === 'POST') {
        const model: AiModel = { ...JSON.parse(String(init.body)), id: 3, revision: 1 };
        models.push(model);
        return Response.json(model, { status: 201 });
      }
      const id = Number(target.searchParams.get('provider_id'));
      return Response.json({ items: id ? models.filter(model => model.provider_id === id) : models, next_cursor: null });
    }
    return Response.json({ items: [], next_cursor: null });
  }));
}

it.each([
  { button: '新增供应商', field: '供应商名称', secret: 'API 密钥', inline: false, view: <Providers token="offline" /> },
  { button: '新增 Agent', field: '名称', secret: '', inline: true, view: <Agents token="offline" /> },
  { button: '新增 Skill', field: '名称', secret: '', inline: false, view: <Skills token="offline" /> },
  { button: '新增 远程服务', field: '名称', secret: '访问令牌', inline: false, view: <McpServers token="offline" /> },
])('$button 取消后重新打开清空草稿', async ({ button, field, secret, inline, view }) => {
  catalogApi();
  render(view);
  await waitFor(() => expect(screen.getByRole('button', { name: button })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: button }));
  const role = inline ? 'form' : 'dialog';
  const name = inline ? 'Agent 配置' : button;
  const editor = await screen.findByRole(role, { name });
  fireEvent.change(within(editor).getByLabelText(field), { target: { value: '取消的草稿' } });
  if (secret) fireEvent.change(within(editor).getByLabelText(secret), { target: { value: 'discarded-secret' } });
  // Agent 的新建生命周期归页内表单管理，其余目录继续保留弹窗交互。
  if (inline) {
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    fireEvent.change(within(editor).getByLabelText('角色'), { target: { value: '取消的角色' } });
    fireEvent.change(within(editor).getByLabelText('性格'), { target: { value: '取消的性格' } });
  }
  fireEvent.click(within(editor).getByRole('button', { name: inline ? '取消新建' : /取\s*消/ }));
  await waitFor(() => expect(screen.queryByRole(role, { name })).not.toBeInTheDocument());
  fireEvent.click(screen.getByRole('button', { name: button }));
  const reopened = await screen.findByRole(role, { name });
  expect(within(reopened).getByLabelText(field)).toHaveValue('');
  if (secret) expect(within(reopened).getByLabelText(secret)).toHaveValue('');
  if (inline) {
    expect(within(reopened).getByLabelText('角色')).toHaveValue('');
    expect(within(reopened).getByLabelText('性格')).toHaveValue('');
  }
});

it('左侧选择供应商只显示当前供应商的模型和启用开关', async () => {
  catalogApi();
  render(<Providers token="offline" />);
  expect(await screen.findByText('upstream-1')).toBeVisible();
  expect(screen.queryByText('upstream-2')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('tab', { name: /供应商2/ }));
  expect(await screen.findByRole('switch', { name: '启用 upstream-2' })).toBeVisible();
  expect(screen.queryByText('upstream-1')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('tab', { name: /供应商1/ }));
  expect(await screen.findByRole('switch', { name: '启用 upstream-1' })).toBeVisible();
  expect(screen.queryByText('upstream-2')).not.toBeInTheDocument();
});

it('新增供应商不在当前分页时仍选中它，避免把模型登记到其他供应商', async () => {
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    if (String(url).includes('/ai/providers')) {
      if (init?.method === 'POST') return Response.json({ ...providers[0], ...JSON.parse(String(init.body)), id: 99 });
      if (String(url).includes('/upstream-models')) return Response.json({ provider_id: 99, revision: 1, models: [] });
      return Response.json({ items: [providers[0]], next_cursor: '1' });
    }
    if (String(url).endsWith('/status')) return Response.json({ python_media: { initialized: false } });
    return Response.json({ items: [], next_cursor: null });
  }));
  render(<Providers token="offline" />);
  await waitFor(() => expect(screen.getByRole('button', { name: '新增供应商' })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: '新增供应商' }));
  const dialog = await screen.findByRole('dialog', { name: '新增供应商' });
  fireEvent.change(within(dialog).getByLabelText('供应商名称'), { target: { value: '刚新增' } });
  fireEvent.change(within(dialog).getByLabelText('API 根地址'), { target: { value: 'https://new.invalid/v1' } });
  fireEvent.click(within(dialog).getByRole('button', { name: '保存供应商' }));
  expect(await screen.findByRole('heading', { name: '刚新增 的模型' })).toBeVisible();
  expect(screen.getByRole('tab', { name: /刚新增/ })).toHaveAttribute('aria-selected', 'true');
});
