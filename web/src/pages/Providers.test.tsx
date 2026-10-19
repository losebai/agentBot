import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { Providers } from './Providers';

it('供应商冲突后保留编辑值且不回填数据库密钥', async () => {
  vi.stubGlobal('fetch', vi.fn(async (url, options) => options?.method === 'PATCH'
    ? Response.json({code: 'AI_CONFIG_CONFLICT', message: '配置冲突'}, {status:409})
    : Response.json({items:String(url).includes('/ai/providers?')?[{id:1,revision:1,name:'本机',protocol:'ollama',base_url:'http://localhost:11434',
      credential_set:true,credential_available:true,enabled:false,description:''}]:[],next_cursor:null})));
  render(<Providers token="offline" />);
  fireEvent.click(await screen.findByRole('button', {name:'编辑本机'}));
  fireEvent.change(screen.getByLabelText('供应商名称'), {target:{value:'修改后的供应商'}});
  fireEvent.click(screen.getByRole('button', {name:'保存供应商'}));
  expect(await screen.findByText('配置冲突')).toBeInTheDocument();
  expect(screen.getByLabelText('供应商名称')).toHaveValue('修改后的供应商');
  expect(screen.getByLabelText('API 密钥')).toHaveValue('');
});

it.each([
  { fail: false, conflict: false }, { fail: true, conflict: false }, { fail: false, conflict: true },
])('保存供应商后自动同步，无需二次导入；失败=$fail、别名冲突=$conflict', async ({ fail, conflict }) => {
  const provider = { id: 2, revision: 1, name: '服务乙', protocol: 'openai-chat', base_url: 'https://two.invalid/v1',
    credential_set: true, credential_available: true, enabled: true, description: '' };
  let saved = false, failDiscovery = fail;
  const models: { id: number; revision: number; alias: string; upstream_model: string; provider_id: number; enabled: boolean }[] = [];
  const fetcher = vi.fn(async (url, options) => {
    if (String(url).includes('upstream-models')) return failDiscovery
      ? Response.json({ message: '获取失败' }, { status: 502 })
      : Response.json({ provider_id: 2, revision: 1, models: ['same-model', 'other-model'] });
    if (String(url).includes('models/import')) {
      models.push(...JSON.parse(String(options.body)).models.filter((item: { alias: string }) => !conflict || item.alias !== 'same-model').map((item: { alias: string; upstream_model: string }, i: number) =>
        ({ ...item, id: i + 1, revision: 1, provider_id: 2, enabled: false })));
      return Response.json({ provider_id: 2, revision: 1, imported: models, skipped: [],
        conflicts: conflict ? [{ alias: 'same-model', upstream_model: 'same-model' }] : [] });
    }
    if (options?.method === 'POST') { saved = true; return Response.json(provider, { status: 201 }); }
    return Response.json({ items: String(url).includes('/ai/providers?') ? (saved ? [provider] : []) : models, next_cursor: null });
  });
  vi.stubGlobal('fetch', fetcher); render(<Providers token="offline" />);
  await waitFor(() => expect(screen.getByRole('button', { name: '新增供应商' })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: '新增供应商' }));
  fireEvent.change(screen.getByLabelText('供应商名称'), { target: { value: '服务乙' } });
  fireEvent.change(screen.getByLabelText('API 根地址'), { target: { value: 'https://two.invalid/v1' } });
  fireEvent.change(screen.getByLabelText('API 密钥'), { target: { value: 'second-key' } });
  fireEvent.click(screen.getByRole('button', { name: '保存供应商' }));
  if (fail) {
    expect(await screen.findByText(/供应商已保存.*获取失败/)).toBeVisible();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    failDiscovery = false;
    fireEvent.click(screen.getByRole('button', { name: '刷新模型' }));
  }
  if (conflict) {
    expect(await screen.findByRole('button', { name: '设置 same-model 的别名并导入' })).toBeVisible();
    expect(screen.queryByRole('switch', { name: '启用 same-model' })).not.toBeInTheDocument();
  } else expect(await screen.findByRole('switch', { name: '启用 same-model' })).not.toBeChecked();
  expect(screen.getByRole('switch', { name: '启用 other-model' })).not.toBeChecked();
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  if (!conflict) expect(screen.queryByRole('button', { name: /导入/ })).not.toBeInTheDocument();
  const requests = fetcher.mock.calls.filter(([url, options]) => options?.method === 'POST' && String(url).endsWith('/ai/providers'));
  expect(requests).toHaveLength(1);
  expect(JSON.parse(String(requests[0][1].body)).credential).toBe('second-key');
});

it('模型同步版本冲突后刷新供应商信息，再次点击使用新版本且不自动重试写入', async () => {
  let provider = { id: 1, revision: 1, name: '旧名称', protocol: 'ollama', base_url: 'http://offline.invalid',
    credential_set: false, credential_available: true, enabled: true, description: '' };
  const revisions: number[] = [];
  const models: { id: number; revision: number; alias: string; upstream_model: string; provider_id: number; enabled: boolean }[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url, options) => {
    if (String(url).includes('upstream-models')) {
      const revision = Number(new URL(String(url), 'http://offline.test').searchParams.get('expected_revision'));
      revisions.push(revision);
      if (revision === 1) {
        provider = { ...provider, revision: 2, name: '已更新名称' };
        return Response.json({ code: 'AI_CONFIG_CONFLICT', message: '供应商版本冲突' }, { status: 409 });
      }
      return Response.json({ provider_id: 1, revision: 2, models: ['fresh'] });
    }
    if (String(url).includes('models/import')) {
      const body = JSON.parse(String(options.body));
      expect(body.expected_revision).toBe(2);
      models.push({ ...body.models[0], id: 1, revision: 1, provider_id: 1, enabled: false });
      return Response.json({ provider_id: 1, revision: 2, imported: models, skipped: [], conflicts: [] });
    }
    return Response.json({ items: String(url).includes('/ai/providers?') ? [provider] : models, next_cursor: null });
  }));
  render(<Providers token="offline" />);
  await waitFor(() => expect(screen.getByRole('button', { name: '刷新模型' })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: '刷新模型' }));
  expect(await screen.findByRole('heading', { name: '已更新名称 的模型' })).toBeVisible();
  expect(await screen.findByText(/供应商配置已重新加载/)).toBeVisible();
  expect(revisions).toEqual([1]);
  expect(models).toHaveLength(0);
  fireEvent.click(screen.getByRole('button', { name: '刷新模型' }));
  expect(await screen.findByRole('switch', { name: '启用 fresh' })).not.toBeChecked();
  expect(revisions).toEqual([1, 2]);
});
