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

it.each([false, true])('保存供应商后自动同步，无需二次导入；失败=%s 时保留供应商并可刷新', async fail => {
  const provider = { id: 2, revision: 1, name: '服务乙', protocol: 'openai-chat', base_url: 'https://two.invalid/v1',
    credential_set: true, credential_available: true, enabled: true, description: '' };
  let saved = false, failDiscovery = fail;
  const models: { id: number; revision: number; alias: string; upstream_model: string; provider_id: number; enabled: boolean }[] = [];
  const fetcher = vi.fn(async (url, options) => {
    if (String(url).includes('upstream-models')) return failDiscovery
      ? Response.json({ message: '获取失败' }, { status: 502 })
      : Response.json({ provider_id: 2, revision: 1, models: ['same-model', 'other-model'] });
    if (String(url).includes('models/import')) {
      models.push(...JSON.parse(String(options.body)).models.map((item: { alias: string; upstream_model: string }, i: number) =>
        ({ ...item, id: i + 1, revision: 1, provider_id: 2, enabled: false })));
      return Response.json({ provider_id: 2, revision: 1, imported: models, skipped: [] });
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
  expect(await screen.findByRole('switch', { name: '启用 2/same-model' })).not.toBeChecked();
  expect(screen.getByRole('switch', { name: '启用 2/other-model' })).not.toBeChecked();
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /导入/ })).not.toBeInTheDocument();
  const requests = fetcher.mock.calls.filter(([url, options]) => options?.method === 'POST' && String(url).endsWith('/ai/providers'));
  expect(requests).toHaveLength(1);
  expect(JSON.parse(String(requests[0][1].body)).credential).toBe('second-key');
});
