import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { Plugins } from './Plugins';

/** 内置插件的保存状态和进程状态由离线响应分别提供。 */
function pluginResponse(saved: boolean) {
  return [{ id: 'qqbot', name: 'QQBot', version: '0.5.0', description: 'QQ 消息插件',
    saved_enabled: saved, enabled: true, status: 'running', restart_required: !saved }];
}

it('QQ 插件卡片提供启动与登录入口，启动后显示所属状态', async () => {
  let started = false;
  const status = () => ({ mode: 'managed', state: started ? 'running' : 'not_started', webui_ready: started, error_code: null });
  const fetcher = vi.fn(async (url, options) => {
    const path = String(url);
    if (path.endsWith('/napcat/start')) { started = true; return Response.json(status()); }
    if (path.endsWith('/napcat/login')) return Response.json({ accepted: true });
    if (path.endsWith('/napcat/plugin')) return Response.json({ available: true, plugin_enabled: true, status: status() });
    if (path.endsWith('/plugins')) return Response.json(pluginResponse(true));
    return Response.json({ database_revision: 3, applied_revision: 3, fields: [] });
  });
  vi.stubGlobal('fetch', fetcher);
  render(<Plugins token="offline" />);
  const start = await screen.findByRole('button', { name: '启动 NapCat' });
  await waitFor(() => expect(start).toBeEnabled());
  expect(screen.getByRole('button', { name: '打开登录页' })).toBeDisabled();
  fireEvent.click(start);
  const login = await screen.findByRole('button', { name: '打开登录页' });
  await waitFor(() => expect(login).toBeEnabled());
  fireEvent.click(login);
  await waitFor(() => expect(fetcher.mock.calls.some(([url, options]) => String(url).endsWith('/napcat/login') && options?.method === 'POST')).toBe(true));
  expect(fetcher.mock.calls.filter(([url]) => String(url).endsWith('/napcat/start'))).toHaveLength(1);
});

it('修改开关不立即启停，保存使用数据库版本并提示重启', async () => {
  let saved = true;
  const fetcher = vi.fn(async (url, options) => {
    if (options?.method === 'PATCH') saved = false;
    return String(url).endsWith('/plugins') ? Response.json(pluginResponse(saved))
      : Response.json({ database_revision: saved ? 3 : 4, applied_revision: 3,
        restart_required_fields: saved ? [] : ['app_enabled_plugins'], fields: [] });
  });
  vi.stubGlobal('fetch', fetcher);
  render(<Plugins token="offline" />);
  fireEvent.click(await screen.findByRole('switch', { name: '启用 QQBot' }));
  expect(fetcher.mock.calls.filter(([, options]) => options?.method === 'PATCH')).toHaveLength(0);
  fireEvent.click(screen.getByRole('button', { name: /保\s*存/ }));
  expect(await screen.findByText('重启后生效')).toBeVisible();
  const writes = fetcher.mock.calls.filter(([, options]) => options?.method === 'PATCH');
  expect(writes).toHaveLength(1);
  expect(JSON.parse(String(writes[0][1]?.body))).toEqual({
    expected_revision: 3, values: { app_enabled_plugins: [] },
  });
  expect(screen.getByText('运行中')).toBeVisible();
});

it('版本冲突后保留草稿并显示错误，不假定插件已停用', async () => {
  vi.stubGlobal('fetch', vi.fn(async (url, options) => {
    if (options?.method === 'PATCH') return Response.json({ message: '配置已被修改，请重新加载' }, { status: 409 });
    return String(url).endsWith('/plugins') ? Response.json(pluginResponse(true))
      : Response.json({ database_revision: 3, applied_revision: 3, restart_required_fields: [], fields: [] });
  }));
  render(<Plugins token="offline" />);
  fireEvent.click(await screen.findByRole('switch', { name: '启用 QQBot' }));
  fireEvent.click(screen.getByRole('button', { name: /保\s*存/ }));
  expect(await screen.findByText('配置已被修改，请重新加载')).toBeVisible();
  expect(screen.getByRole('switch', { name: '启用 QQBot' })).not.toBeChecked();
  await waitFor(() => expect(screen.getByRole('button', { name: /保\s*存/ })).toBeEnabled());
});
