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

it('OpenAI 供应商密钥必填并可点击显示或隐藏', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => Response.json({ items: [], next_cursor: null, total: 0 })));
  render(<Providers token="offline" />);
  await waitFor(() => expect(screen.getByRole('button', { name: '新增供应商' })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: '新增供应商' }));
  const credential = screen.getByLabelText('API 密钥');
  expect(credential).toBeRequired();
  expect(credential).toHaveAttribute('type', 'password');
  fireEvent.change(credential, { target: { value: 'private-key' } });
  fireEvent.click(screen.getByRole('img', { name: 'eye-invisible' }));
  expect(credential).toHaveAttribute('type', 'text');
  fireEvent.click(screen.getByRole('img', { name: 'eye' }));
  expect(credential).toHaveAttribute('type', 'password');
  expect(screen.queryByLabelText(/清除API 密钥/)).not.toBeInTheDocument();
});

it('编辑供应商时自动隐藏显示已保存密钥，关闭后重新读取', async () => {
  const provider = { id: 1, revision: 3, name: '远程服务', protocol: 'openai-chat',
    base_url: 'https://offline.invalid/v1', user_agent: '', credential_set: true,
    credential_available: true, enabled: true, description: '' };
  const fetcher = vi.fn(async url => {
    const path = String(url);
    if (path.includes('/credential?')) return Response.json({ credential: 'saved-private-key' });
    if (path.includes('upstream-models')) return Response.json({ provider_id: 1, revision: 3, models: [] });
    return Response.json({ items: path.includes('/ai/providers?') ? [provider] : [], next_cursor: null, total: 1 });
  });
  vi.stubGlobal('fetch', fetcher);
  render(<Providers token="offline" />);
  fireEvent.click(await screen.findByRole('button', { name: '编辑远程服务' }));
  let credential = screen.getByLabelText('API 密钥');
  await waitFor(() => expect(credential).toHaveValue('saved-private-key'));
  expect(credential).toHaveAttribute('type', 'password');
  fireEvent.click(screen.getByRole('img', { name: 'eye-invisible' }));
  expect(credential).toHaveAttribute('type', 'text');
  fireEvent.click(screen.getByRole('img', { name: 'eye' }));
  expect(credential).toHaveValue('saved-private-key');
  expect(credential).toHaveAttribute('type', 'password');
  fireEvent.click(screen.getByRole('button', { name: /取\s*消/ }));
  fireEvent.click(screen.getByRole('button', { name: '编辑远程服务' }));
  credential = screen.getByLabelText('API 密钥');
  await waitFor(() => expect(credential).toHaveValue('saved-private-key'));
  expect(credential).toHaveAttribute('type', 'password');
  expect(fetcher.mock.calls.filter(([url]) => String(url).includes('/credential?'))).toHaveLength(2);
}, 10000);

it.each([false, true])('保存供应商后不自动访问上游，显式获取并保存模型；失败=%s', async (fail) => {
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
      return Response.json({ provider_id: 2, revision: 1, imported: models, skipped: [],
        conflicts: [] });
    }
    if (options?.method === 'POST') { saved = true; return Response.json(provider, { status: 201 }); }
    return Response.json({ items: String(url).includes('/ai/providers?') ? (saved ? [provider] : []) : models, next_cursor: null });
  });
  vi.stubGlobal('fetch', fetcher); render(<Providers token="offline" />);
  await waitFor(() => expect(screen.getByRole('button', { name: '新增供应商' })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: '新增供应商' }));
  fireEvent.change(screen.getByLabelText('供应商名称'), { target: { value: '服务乙' } });
  fireEvent.change(screen.getByLabelText('API 根地址'), { target: { value: 'https://two.invalid/v1' } });
  fireEvent.change(screen.getByLabelText('User-Agent'), { target: { value: 'AgentBot-Web/1.0' } });
  fireEvent.change(screen.getByLabelText('API 密钥'), { target: { value: 'second-key' } });
  fireEvent.click(screen.getByRole('button', { name: '保存供应商' }));
  await waitFor(() => expect(screen.getByRole('button', { name: '获取上游模型列表' })).toBeEnabled());
  expect(fetcher.mock.calls.filter(([url]) => String(url).includes('upstream-models'))).toHaveLength(0);
  fireEvent.click(screen.getByRole('button', { name: '获取上游模型列表' }));
  if (fail) {
    expect(await screen.findByText(/模型同步失败.*获取失败/)).toBeVisible();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    failDiscovery = false;
    await waitFor(() => expect(screen.getByRole('button', { name: '获取上游模型列表' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: '获取上游模型列表' }));
  }
  expect(await screen.findByRole('switch', { name: '启用 same-model' })).not.toBeChecked();
  expect(screen.getByRole('switch', { name: '启用 other-model' })).not.toBeChecked();
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /导入/ })).not.toBeInTheDocument();
  const requests = fetcher.mock.calls.filter(([url, options]) => options?.method === 'POST' && String(url).endsWith('/ai/providers'));
  expect(requests).toHaveLength(1);
  expect(JSON.parse(String(requests[0][1].body)).credential).toBe('second-key');
  expect(JSON.parse(String(requests[0][1].body)).user_agent).toBe('AgentBot-Web/1.0');
}, 10000);

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
  expect(await screen.findByRole('heading', { name: '旧名称 的模型' })).toBeVisible();
  expect(revisions).toEqual([]);
  fireEvent.click(screen.getByRole('button', { name: '获取上游模型列表' }));
  expect(await screen.findByRole('heading', { name: '已更新名称 的模型' })).toBeVisible();
  expect(await screen.findByText(/供应商配置已重新加载/)).toBeVisible();
  expect(revisions).toEqual([1]);
  expect(models).toHaveLength(0);
  fireEvent.click(screen.getByRole('button', { name: '获取上游模型列表' }));
  expect(await screen.findByRole('switch', { name: '启用 fresh' })).not.toBeChecked();
  expect(revisions).toEqual([1, 2]);
});

it('打开和切换供应商只读本地目录，只有按钮显式获取上游模型列表', async () => {
  const reads: number[] = [];
  const providers = [1, 2].map(id => ({ id, revision: 1, name: `供应商${id}`, protocol: 'ollama',
    base_url: 'http://offline.invalid', credential_set: false, credential_available: true, enabled: true, description: '' }));
  vi.stubGlobal('fetch', vi.fn(async url => {
    const path = String(url);
    if (path.includes('upstream-models')) {
      const id = Number(path.split('/')[5]); reads.push(id);
      return Response.json({ provider_id: id, revision: 1, models: [] });
    }
    return Response.json({ items: path.includes('/ai/providers?') ? providers : [], next_cursor: null });
  }));
  render(<Providers token="offline" />);
  await waitFor(() => expect(screen.getByRole('button', { name: '获取上游模型列表' })).toBeEnabled());
  expect(reads).toEqual([]);
  fireEvent.click(screen.getByRole('button', { name: '获取上游模型列表' }));
  await waitFor(() => expect(reads).toEqual([1]));
  fireEvent.click(screen.getByRole('tab', { name: /供应商2/ }));
  await waitFor(() => expect(screen.getByRole('heading', { name: '供应商2 的模型' })).toBeVisible());
  expect(reads).toEqual([1]);
  fireEvent.click(screen.getByRole('tab', { name: /供应商2/ }));
  await waitFor(() => expect(screen.getByRole('button', { name: '获取上游模型列表' })).toBeEnabled());
  expect(reads).toEqual([1]);
  fireEvent.click(screen.getByRole('button', { name: '获取上游模型列表' }));
  await waitFor(() => expect(reads).toEqual([1, 2]));
}, 10000);

it('供应商目录自动读取所有游标页并只使用滚动列表', async () => {
  const providers = Array.from({ length: 12 }, (_, index) => ({ id: index + 1, revision: 1,
    name: `供应商${index + 1}`, protocol: 'ollama', base_url: 'http://offline.invalid',
    credential_set: false, credential_available: true, enabled: true, description: '' }));
  const cursors: (string | null)[] = [];
  vi.stubGlobal('fetch', vi.fn(async url => {
    const request = new URL(String(url), 'http://offline.test');
    if (request.pathname.includes('upstream-models')) return Response.json({
      provider_id: Number(request.pathname.split('/')[5]), revision: 1, models: [],
    });
    if (request.pathname.endsWith('/ai/providers')) {
      const cursor = request.searchParams.get('cursor'); cursors.push(cursor);
      const start = cursor ? 10 : 0;
      return Response.json({ items: providers.slice(start, start + 10),
        next_cursor: cursor ? null : '10', total: providers.length });
    }
    return Response.json({ items: [], next_cursor: null, total: 0 });
  }));
  render(<Providers token="offline" />);
  expect(await screen.findByRole('tab', { name: /供应商12/ })).toBeVisible();
  expect(screen.getAllByRole('tab')).toHaveLength(12);
  expect(document.querySelector('.provider-list .ant-pagination')).not.toBeInTheDocument();
  expect(cursors).toEqual([null, '10']);
});

it('保存供应商后显式获取使用新版本，不等待列表重读或使用旧快照', async () => {
  let provider = { id: 1, revision: 1, name: '供应商', protocol: 'ollama', base_url: 'http://offline.invalid',
    credential_set: false, credential_available: true, enabled: true, description: '' };
  const revisions: number[] = [];
  let finishList!: (response: Response) => void;
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    const path = String(url);
    if (init?.method === 'PATCH') {
      provider = { ...provider, revision: 2, name: '已修改' };
      return Response.json(provider);
    }
    if (path.includes('upstream-models')) {
      revisions.push(Number(new URL(path, 'http://offline.test').searchParams.get('expected_revision')));
      return Response.json({ provider_id: 1, revision: provider.revision, models: [] });
    }
    if (path.includes('/ai/providers?')) {
      if (provider.revision === 2) return new Promise<Response>(resolve => { finishList = resolve; });
      return Response.json({ items: [provider], next_cursor: null });
    }
    return Response.json({ items: [], next_cursor: null });
  }));
  render(<Providers token="offline" />);
  await waitFor(() => expect(screen.getByRole('heading', { name: '供应商 的模型' })).toBeVisible());
  expect(revisions).toEqual([]);
  fireEvent.click(screen.getByRole('button', { name: '编辑供应商' }));
  fireEvent.change(screen.getByLabelText('供应商名称'), { target: { value: '已修改' } });
  fireEvent.click(screen.getByRole('button', { name: '保存供应商' }));
  expect(await screen.findByRole('heading', { name: '已修改 的模型' })).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '获取上游模型列表' }));
  await waitFor(() => expect(revisions).toEqual([2]));
  finishList(Response.json({ items: [provider], next_cursor: null }));
  expect(revisions).toEqual([2]);
});
