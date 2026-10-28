import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { App } from '../App';
import { PluginConfiguration } from './PluginConfiguration';

/** 只读管理接口替身：扫码与 NapCat 操作必须由独立页面显式触发。 */
function setup(running = true) {
  const fetcher = vi.fn(async (url, options) => {
    const path = String(url);
    if (path.endsWith('/auth')) return Response.json({ password_required: false });
    if (path.endsWith('/plugins')) return Response.json(['qqbot', 'bilibili', 'comfy'].map(id => ({
      id, name: id === 'qqbot' ? 'QQBot' : id === 'comfy' ? 'ComfyUI' : 'B站',
      version: 'offline', description: '内置平台插件',
      enabled: running, saved_enabled: true, status: running ? 'running' : 'disabled', restart_required: !running,
    })));
    if (path.endsWith('/settings')) return Response.json({ database_revision: 1, applied_revision: 1,
      restart_required_fields: [], startup_revision: 'offline-file', fields: [
        { name: 'nickname', description: '机器人名称', value: ['小助手'], kind: 'json', scope: 'runtime',
          nullable: false, secret_is_set: false, restart_required: false },
        { name: 'port', description: '监听端口', value: 8080, kind: 'number', scope: 'static',
          read_only: true, nullable: false, secret_is_set: false, restart_required: true },
        { name: 'chat_cooldown_seconds', description: '自动回复冷却', value: 2, kind: 'number', scope: 'runtime',
          nullable: false, secret_is_set: false, restart_required: false },
        { name: 'chat_local_workflow_base_url', description: 'Comfy 服务地址', value: 'http://127.0.0.1:8188',
          kind: 'string', scope: 'runtime', nullable: false, secret_is_set: false, restart_required: false },
      ] });
    if (path.endsWith('/napcat/plugin')) return Response.json({ available: false, plugin_enabled: true,
      status: { mode: 'external', state: 'not_started', webui_ready: false, error_code: null } });
    if (path.endsWith('/bilibili/status')) return Response.json({ logged_in: false, status: 'not_logged_in' });
    if (path.endsWith('/bilibili/login/qr') && options?.method === 'POST') return Response.json({
      session_id: 'offline', status: 'waiting', expires_at: 9999999999,
      url: 'https://account.bilibili.com/h5/account-h5/auth/scan-web?qrcode_key=offline',
    });
    if (path.includes('/bilibili/login/qr/')) return Response.json({ status: 'waiting' });
    if (path.endsWith('/bots')) return Response.json([]);
    if (path.includes('/queues/')) return Response.json({ available: false, text: 0, images: 0, total: 0 });
    return Response.json({ service: 'agentBot', ready: true, bot_count: 0,
      database_revision: 1, applied_revision: 1, restart_required_fields: [] });
  });
  vi.stubGlobal('fetch', fetcher);
  return fetcher;
}

it('插件管理提供二级中心和已安装入口，列表不包含插件配置表单', async () => {
  const fetcher = setup();
  render(<App />);
  const nav = await screen.findByRole('navigation', { name: '主导航' });
  fireEvent.click(await within(nav).findByRole('button', { name: '插件中心' }));
  expect(await screen.findByRole('heading', { name: '插件中心' })).toBeVisible();
  fireEvent.click(within(nav).getByRole('button', { name: '已安装插件' }));
  expect(await screen.findByRole('heading', { name: '已安装插件' })).toBeVisible();
  expect(await screen.findByRole('switch', { name: '启用 B站' })).toBeVisible();
  expect(screen.queryByRole('button', { name: '扫码登录' })).not.toBeInTheDocument();
  expect(fetcher.mock.calls.some(([url]) => String(url).endsWith('/bilibili/status'))).toBe(false);
});

it('B站有独立配置页，离开页面取消当前二维码轮询', async () => {
  const fetcher = setup();
  render(<App />);
  const nav = await screen.findByRole('navigation', { name: '主导航' });
  fireEvent.click(await within(nav).findByRole('button', { name: 'B站配置' }));
  fireEvent.click(await screen.findByRole('button', { name: '扫码登录' }));
  await waitFor(() => expect(fetcher.mock.calls.some(([url]) => String(url).endsWith('/bilibili/login/qr/offline'))).toBe(true));
  const signal = fetcher.mock.calls.find(([url]) => String(url).endsWith('/bilibili/login/qr/offline'))?.[1]?.signal;
  fireEvent.click(within(nav).getByRole('button', { name: '插件中心' }));
  expect(signal?.aborted).toBe(true);
  expect(screen.queryByRole('region', { name: 'B站账号' })).not.toBeInTheDocument();
});

it('保存待启用但尚未运行时，不显示独立配置二级菜单', async () => {
  setup(false);
  render(<App />);
  const nav = await screen.findByRole('navigation', { name: '主导航' });
  fireEvent.click(await within(nav).findByRole('button', { name: '已安装插件' }));
  await screen.findByRole('switch', { name: '启用 B站' });
  expect(within(nav).queryByRole('button', { name: 'B站配置' })).not.toBeInTheDocument();
  expect(within(nav).queryByRole('button', { name: 'QQ 配置' })).not.toBeInTheDocument();
  expect(within(nav).queryByRole('button', { name: 'Comfy 配置' })).not.toBeInTheDocument();
});

it('Comfy 连接与工作流归属插件配置，离开返回保留连接草稿', async () => {
  setup();
  render(<App />);
  const nav = await screen.findByLabelText('主导航');
  fireEvent.click(await within(nav).findByText('Comfy 配置', { selector: 'button' }));
  const address = await screen.findByLabelText('Comfy 服务地址');
  fireEvent.change(address, { target: { value: 'http://127.0.0.1:9000' } });
  expect(screen.getByText('工作流管理', { selector: '[role="tab"]' })).toBeVisible();
  fireEvent.click(within(nav).getByText('机器人配置', { selector: 'button' }));
  const settingsPage = screen.getByText('机器人配置', { selector: 'h1' }).closest<HTMLElement>('.page-content')!;
  expect(within(settingsPage).queryByText('Comfy', { selector: 'button' })).not.toBeInTheDocument();
  expect(screen.queryByLabelText('Comfy 服务地址')).not.toBeVisible();
  fireEvent.click(await within(nav).findByText('Comfy 配置', { selector: 'button' }));
  await waitFor(() => expect(screen.getByLabelText('Comfy 服务地址')).toBeVisible());
  expect(screen.getByLabelText('Comfy 服务地址')).toHaveValue('http://127.0.0.1:9000');
});

it('插件状态刷新保留当前 QQ 设置草稿，不把状态检查当作丢弃修改', async () => {
  setup();
  render(<PluginConfiguration token="offline" pluginId="qqbot" onInstalled={() => {}} />);
  const nickname = await screen.findByLabelText('机器人名称');
  fireEvent.change(nickname, { target: { value: '未保存名字' } });
  const refresh = screen.getByText('重新加载插件', { selector: 'button > span' }).closest('button')!;
  fireEvent.click(refresh);
  await waitFor(() => expect(refresh).not.toHaveClass('ant-btn-loading'));
  expect(await screen.findByLabelText('机器人名称')).toHaveValue('未保存名字');
});

it('离开QQ配置保留草稿，同时取消所属 NapCat 读取', async () => {
  const fetcher = setup();
  render(<App />);
  const nav = await screen.findByLabelText('主导航');
  fireEvent.click(await within(nav).findByText('QQ 配置', { selector: 'button' }));
  const nickname = await screen.findByLabelText('机器人名称');
  fireEvent.change(nickname, { target: { value: '跨页面草稿' } });
  const signal = fetcher.mock.calls.find(([url]) => String(url).endsWith('/napcat/plugin'))?.[1]?.signal;
  fireEvent.click(within(nav).getByText('插件中心', { selector: 'button' }));
  expect(nickname).not.toBeVisible();
  expect(signal?.aborted).toBe(true);
  fireEvent.click(await within(nav).findByText('QQ 配置', { selector: 'button' }));
  expect(await screen.findByLabelText('机器人名称')).toHaveValue('跨页面草稿');
});

it('返回配置页先验证本次运行状态，不按离开前缓存提前读取账号服务', async () => {
  const fetcher = setup();
  const response = fetcher.getMockImplementation()!;
  let delayed = false;
  let complete: ((value: Response) => void) | undefined;
  fetcher.mockImplementation(async (url, options) => {
    if (delayed && String(url).endsWith('/plugins')) return new Promise<Response>(resolve => { complete = resolve; });
    return response(url, options);
  });
  const view = render(<PluginConfiguration token="offline" pluginId="qqbot" onInstalled={() => {}} />);
  const nickname = await screen.findByLabelText('机器人名称');
  view.rerender(<PluginConfiguration token="offline" pluginId="qqbot" active={false} onInstalled={() => {}} />);
  delayed = true;
  view.rerender(<PluginConfiguration token="offline" pluginId="qqbot" active onInstalled={() => {}} />);
  await waitFor(() => expect(complete).toBeDefined());
  expect(screen.queryByLabelText('QQ NapCat 接入')).not.toBeInTheDocument();
  expect(nickname).not.toBeVisible();
  await act(async () => complete!(Response.json([{ id: 'qqbot', enabled: false, saved_enabled: false, status: 'disabled' }])));
  expect(await screen.findByText('插件未运行，请在已安装插件中启用并手动重启后端。')).toBeVisible();
  expect(fetcher.mock.calls.filter(([url]) => String(url).endsWith('/napcat/plugin'))).toHaveLength(1);
});

it('QQ 设置移入独立配置，共享配置不再重复展示 QQ 分类', async () => {
  const fetcher = setup();
  render(<App />);
  const nav = await screen.findByLabelText('主导航');
  fireEvent.click(await within(nav).findByText('QQ 配置', { selector: 'button' }));
  expect(await screen.findByLabelText('QQ NapCat 接入')).toBeVisible();
  const nickname = await screen.findByLabelText('机器人名称');
  expect(nickname).toBeVisible();
  const categories = await screen.findByLabelText('机器人配置分类');
  expect(within(categories).queryByText('LLM设置', { selector: 'button' })).not.toBeInTheDocument();
  expect(within(categories).queryByText('自动回复设置', { selector: 'button' })).not.toBeInTheDocument();
  fireEvent.change(nickname, { target: { value: '新名字' } });
  fireEvent.click(screen.getByText('保存修改', { selector: 'button > span' }).closest('button')!);
  await waitFor(() => expect(fetcher.mock.calls.some(([, options]) => options?.method === 'PATCH')).toBe(true));
  const write = fetcher.mock.calls.find(([, options]) => options?.method === 'PATCH')?.[1];
  expect(JSON.parse(write?.body)).toEqual({ expected_revision: 1, values: { nickname: ['新名字'] } });
  fireEvent.click(within(nav).getByText('机器人配置', { selector: 'button' }));
  expect(await screen.findByLabelText('监听端口')).toBeVisible();
  const sharedCategories = screen.getAllByLabelText('机器人配置分类').find(element => !element.closest('[hidden]'))!;
  expect(within(sharedCategories).queryByText('QQ', { selector: 'button' })).not.toBeInTheDocument();
  expect(screen.getByLabelText('机器人名称')).not.toBeVisible();
});

it.each([true, false])('QQ 运行状态为 %s 时通用机器人配置都可编辑保存自动回复', async running => {
  const fetcher = setup(running);
  render(<App />);
  const nav = await screen.findByLabelText('主导航');
  fireEvent.click(within(nav).getByText('机器人配置', { selector: 'button' }));
  fireEvent.click(await screen.findByText('自动回复设置', { selector: 'button' }));
  const cooldown = await screen.findByLabelText('自动回复冷却');
  fireEvent.change(cooldown, { target: { value: '3' } });
  fireEvent.click(screen.getByText('保存修改', { selector: 'button > span' }).closest('button')!);
  await waitFor(() => expect(fetcher.mock.calls.some(([, options]) => options?.method === 'PATCH')).toBe(true));
  const write = fetcher.mock.calls.find(([, options]) => options?.method === 'PATCH')?.[1];
  expect(JSON.parse(write?.body)).toEqual({ expected_revision: 1, values: { chat_cooldown_seconds: 3 } });
  // 概览允许读取托管状态，此处只确认通用配置没有挂载 QQ 专属接入组件。
  expect(fetcher.mock.calls.some(([url]) => String(url).endsWith('/napcat/plugin'))).toBe(false);
});
