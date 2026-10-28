import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { Plugins } from './Plugins';

/** 内置插件的保存状态和进程状态由离线响应分别提供。 */
function pluginResponse(saved: boolean) {
  return [{ id: 'qqbot', name: 'QQBot', version: '0.5.0', description: 'QQ 消息插件',
    saved_enabled: saved, enabled: true, status: 'running', restart_required: !saved }];
}

it('独立插件进程的启停只读，使用当前命令选择且不发送保存', async () => {
  const fetcher = vi.fn(async (url, _options?: RequestInit) => String(url).endsWith('/plugins')
    ? Response.json(pluginResponse(false).map(item => ({ ...item, restart_required: false })))
    : Response.json({ database_revision: 3, applied_revision: 3, fields: [
      { name: 'app_enabled_plugins', value: [], read_only: true, kind: 'json', scope: 'runtime' },
    ] }));
  vi.stubGlobal('fetch', fetcher);
  render(<Plugins token="offline" />);
  const toggle = await screen.findByRole('switch', { name: '启用 QQBot' });
  expect(toggle).toBeChecked();
  expect(toggle).toBeDisabled();
  expect(screen.getByText(/独立进程/)).toBeVisible();
  expect(screen.getByRole('button', { name: /保\s*存/ })).toBeDisabled();
  expect(fetcher.mock.calls.every(([, options]) => options?.method !== 'PATCH')).toBe(true);
});

it('已安装列表只管理启停并跳转独立配置，不提前挂载账号设置', async () => {
  const configure = vi.fn();
  const fetcher = vi.fn(async url => String(url).endsWith('/plugins')
    ? Response.json(pluginResponse(true))
    : Response.json({ database_revision: 3, applied_revision: 3, fields: [] }));
  vi.stubGlobal('fetch', fetcher);
  render(<Plugins token="offline" onConfigure={configure} />);
  fireEvent.click(await screen.findByRole('button', { name: '配置 QQBot' }));
  expect(configure).toHaveBeenCalledWith('qqbot');
  expect(screen.queryByRole('button', { name: '启动 NapCat' })).not.toBeInTheDocument();
  expect(fetcher.mock.calls.some(([url]) => String(url).includes('/napcat/'))).toBe(false);
});

it('插件中心展示随程序安装的目录，停用不误标为未安装', async () => {
  const installed = vi.fn();
  vi.stubGlobal('fetch', vi.fn(async url => String(url).endsWith('/plugins')
    ? Response.json([{ ...pluginResponse(false)[0], enabled: false, status: 'disabled' }])
    : Response.json({ database_revision: 3, applied_revision: 3, fields: [] })));
  render(<Plugins token="offline" view="center" onInstalled={installed} />);
  expect(await screen.findByRole('heading', { name: '插件中心' })).toBeVisible();
  expect(await screen.findByText('QQBot')).toBeVisible();
  expect(screen.getByText('内置插件')).toBeVisible();
  expect(screen.getByText('已安装')).toBeVisible();
  expect(screen.queryByRole('switch')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '管理 QQBot' }));
  expect(installed).toHaveBeenCalledOnce();
});

it('中心与已安装列表切换保留未保存的启停草稿', async () => {
  vi.stubGlobal('fetch', vi.fn(async url => String(url).endsWith('/plugins')
    ? Response.json(pluginResponse(true))
    : Response.json({ database_revision: 3, applied_revision: 3, fields: [] })));
  const view = render(<Plugins token="offline" />);
  fireEvent.click(await screen.findByRole('switch', { name: '启用 QQBot' }));
  view.rerender(<Plugins token="offline" view="center" />);
  expect(screen.queryByRole('switch')).not.toBeInTheDocument();
  view.rerender(<Plugins token="offline" view="installed" />);
  expect(screen.getByRole('switch', { name: '启用 QQBot' })).not.toBeChecked();
  expect(screen.getByRole('button', { name: /保\s*存/ })).toBeEnabled();
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
