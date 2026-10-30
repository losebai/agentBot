import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { ApiClient } from '../api';
import { ComfyLaunch } from './ComfyLaunch';
import { PluginConfiguration } from '../pages/PluginConfiguration';

const offline = { state: 'offline', configured: true, local: true, managed: false,
  error_code: null, python: 'D:/env/python.exe', directory: 'D:/ComfyUI' };

afterEach(() => vi.useRealTimers());

it('显式启动只发送空意图，使用后端状态显示启动中', async () => {
  const fetcher = vi.fn(async (_url: unknown, options?: RequestInit) => Response.json(
    options?.method === 'POST' ? { ...offline, state: 'starting', managed: true } : offline));
  vi.stubGlobal('fetch', fetcher);
  render(<ComfyLaunch api={new ApiClient('admin')} />);
  const start = await screen.findByRole('button', { name: '启动 ComfyUI' });
  await screen.findByText('未启动');
  fireEvent.click(start);
  expect(await screen.findByText('启动中')).toBeVisible();
  expect(start).toBeDisabled();
  const request = fetcher.mock.calls.find(([, options]) => options?.method === 'POST');
  expect(request?.[0]).toBe('/api/v1/comfy/start');
  expect(request?.[1]?.body).toBe('{}');
});

it.each([
  [{ state: 'not_configured', configured: false }, '未配置'],
  [{ state: 'online', configured: false, local: false }, '在线'],
  [{ local: false }, '未启动'],
])('未配置或外部服务不提供重复启动 %j', async (changes, label) => {
  vi.stubGlobal('fetch', vi.fn(async () => Response.json({ ...offline, ...changes })));
  render(<ComfyLaunch api={new ApiClient('admin')} />);
  expect(await screen.findByText(label)).toBeVisible();
  expect(screen.getByRole('button', { name: '启动 ComfyUI' })).toBeDisabled();
});

it('失败提供端口冲突原因和有效配置，说明保存配置仍需重启', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => Response.json({ ...offline, state: 'failed', error_code: 'port_in_use' })));
  render(<ComfyLaunch api={new ApiClient('admin')} />);
  expect(await screen.findByText(/端口已被/)).toBeVisible();
  expect(screen.getByText(/D:\/env\/python.exe/)).toBeVisible();
  expect(screen.getByText(/D:\/ComfyUI/)).toBeVisible();
  expect(screen.getByText(/保存.*重启后端/)).toBeVisible();
});

it('串行轮询且离开取消读取，忽略取消后返回的旧状态', async () => {
  vi.useFakeTimers();
  let finish!: (value: Response) => void;
  const fetcher = vi.fn((_url: unknown, _options?: RequestInit) => new Promise<Response>(resolve => { finish = resolve; }));
  vi.stubGlobal('fetch', fetcher);
  const view = render(<ComfyLaunch api={new ApiClient('admin')} />);
  await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
  expect(fetcher).toHaveBeenCalledTimes(1);
  const signal = fetcher.mock.calls[0][1]?.signal;
  view.unmount();
  expect(signal?.aborted).toBe(true);
  await act(async () => { finish(Response.json(offline)); await vi.advanceTimersByTimeAsync(6000); });
  expect(fetcher).toHaveBeenCalledTimes(1);
});

it('启动取消进行中的轮询，旧响应不能覆盖启动结果', async () => {
  vi.useFakeTimers();
  let finish!: (value: Response) => void;
  let reads = 0;
  const fetcher = vi.fn(async (_url: unknown, options?: RequestInit) => {
    if (options?.method === 'POST') return Response.json({ ...offline, state: 'starting' });
    if (++reads === 1) return Response.json(offline);
    return new Promise<Response>(resolve => { finish = resolve; });
  });
  vi.stubGlobal('fetch', fetcher);
  render(<ComfyLaunch api={new ApiClient('admin')} />);
  await act(async () => {});
  await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
  fireEvent.click(screen.getByRole('button', { name: '启动 ComfyUI' }));
  await act(async () => {});
  expect(screen.getByText('启动中')).toBeVisible();
  await act(async () => { finish(Response.json(offline)); });
  expect(screen.getByText('启动中')).toBeVisible();
});

it('启动字段归入 Comfy，离开保留草稿且待重启值不冒充生效配置', async () => {
  const snapshot = { database_revision: 1, applied_revision: 1, startup_revision: 'file-1',
    restart_required_fields: [], fields: [
      { name: 'agent_bot_comfy_python', description: 'Comfy Python', kind: 'string',
        value: 'D:/env/python.exe', scope: 'static', read_only: false, nullable: true,
        secret_is_set: false, restart_required: true },
      { name: 'agent_bot_comfy_directory', description: 'Comfy 目录', kind: 'string',
        value: 'D:/ComfyUI', scope: 'static', read_only: false, nullable: true,
        secret_is_set: false, restart_required: true },
      { name: 'agent_bot_comfy_args', description: 'Comfy 附加参数', kind: 'json',
        value: [], scope: 'static', read_only: false, nullable: false,
        secret_is_set: false, restart_required: true },
    ] };
  const fetcher = vi.fn(async (url: unknown, options?: RequestInit) => {
    if (String(url).endsWith('/plugins')) return Response.json([
      { id: 'comfy', enabled: true, saved_enabled: true, status: 'running' },
    ]);
    if (String(url).endsWith('/comfy/status')) return Response.json(offline);
    if (String(url).endsWith('/settings')) return Response.json(options?.method === 'PATCH'
      ? { ...snapshot, startup_revision: 'file-2', restart_required_fields: ['agent_bot_comfy_python'],
        fields: snapshot.fields.map(field => field.name === 'agent_bot_comfy_python'
          ? { ...field, value: 'D:/new/python.exe' } : field) } : snapshot);
    throw new Error(`未预期接口：${url}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const view = render(<PluginConfiguration token="admin" pluginId="comfy" />);
  fireEvent.change(await screen.findByLabelText('Comfy Python'), { target: { value: 'D:/new/python.exe' } });
  expect(screen.getByLabelText('Comfy 目录')).toBeVisible();
  expect(screen.getByLabelText('Comfy 附加参数')).toBeVisible();
  const signal = fetcher.mock.calls.find(([url]) => String(url).endsWith('/comfy/status'))?.[1]?.signal;
  view.rerender(<PluginConfiguration token="admin" pluginId="comfy" active={false} />);
  expect(signal?.aborted).toBe(true);
  expect(screen.queryByRole('region', { name: 'ComfyUI 服务启动' })).not.toBeInTheDocument();
  view.rerender(<PluginConfiguration token="admin" pluginId="comfy" />);
  await waitFor(() => expect(screen.getByLabelText('Comfy Python')).toBeVisible());
  expect(screen.getByLabelText('Comfy Python')).toHaveValue('D:/new/python.exe');
  fireEvent.click(screen.getByRole('button', { name: '保存修改' }));
  expect(await screen.findByText('配置已保存；请查看应用版本与重启提示。')).toBeVisible();
  const saved = fetcher.mock.calls.find(([, options]) => options?.method === 'PATCH');
  expect(JSON.parse(String(saved?.[1]?.body))).toEqual({ expected_revision: 1, startup_revision: 'file-1',
    values: { agent_bot_comfy_python: 'D:/new/python.exe' } });
  const panel = screen.getByRole('region', { name: 'ComfyUI 服务启动' });
  expect(within(panel).getByText(/D:\/env\/python.exe/)).toBeVisible();
  expect(within(panel).queryByText(/D:\/new\/python.exe/)).not.toBeInTheDocument();
});
