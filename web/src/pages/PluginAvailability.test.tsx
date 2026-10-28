import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { Settings } from './Settings';
import { PluginConfiguration } from './PluginConfiguration';
import { AgentToolSelects } from '../components/AgentToolSelects';
import { AgentChat } from '../components/AgentChat';
import type { PluginInfo } from '../types';

/** 保存选择故意保持启用，验证页面只依据当前进程的实际运行状态。 */
function plugin(id: string, status: PluginInfo['status']): PluginInfo {
  return { id, name: id === 'bilibili' ? 'B站' : 'QQBot', version: '0.5.7', description: '',
    enabled: status !== 'disabled', saved_enabled: true, status, restart_required: status === 'disabled' };
}

/** 离线提供真实接口形状，禁止登录、投稿或调用外部平台。 */
function servePlugins(items: PluginInfo[]) {
  vi.stubGlobal('fetch', vi.fn(async url => {
    const path = String(url);
    if (path.endsWith('/plugins')) return Response.json(items);
    if (path.endsWith('/settings')) return Response.json({ database_revision: 1,
      applied_revision: 1, startup_revision: null, restart_required_fields: [], fields: [] });
    if (path.endsWith('/napcat/plugin')) return Response.json({ available: false,
      plugin_enabled: true, status: { mode: 'external', state: 'not_started', webui_ready: false, error_code: null } });
    return Response.json({ items: [], next_cursor: null });
  }));
}

it.each(['disabled', 'prepared', 'failed', 'stopped'] as const)(
  '插件为 %s 时独立配置页也不能挂载账号设置', async status => {
    servePlugins([plugin('bilibili', status), plugin('qqbot', status)]);
    render(<PluginConfiguration token="offline" pluginId="bilibili" />);
    expect(await screen.findByText('插件未运行，请在插件中心启用并手动重启后端。')).toBeVisible();
    expect(screen.queryByRole('region', { name: 'B站账号' })).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'QQ NapCat 接入' })).not.toBeInTheDocument();
  },
);

it('插件运行后显示所属设置，QQ 配置随实际状态重新加载', async () => {
  const items = [plugin('bilibili', 'running'), plugin('qqbot', 'running')];
  servePlugins(items);
  const view = render(<PluginConfiguration token="offline" pluginId="bilibili" />);
  expect(await screen.findByRole('button', { name: '扫码登录' })).toBeEnabled();
  view.unmount();
  render(<Settings token="offline" />);
  expect(await screen.findByRole('button', { name: 'QQ' })).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: 'QQ' }));
  items[1] = plugin('qqbot', 'stopped');
  fireEvent.click(screen.getByRole('button', { name: /重新加载/ }));
  await waitFor(() => expect(screen.queryByRole('button', { name: 'QQ' })).not.toBeInTheDocument());
  expect(screen.getByRole('button', { name: '自动回复设置' })).toBeVisible();
  expect(screen.queryByRole('button', { name: '游戏资讯设置' })).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: '基础设置' })).toHaveAttribute('aria-current', 'page');
});

it('未运行插件的工具不出现在候选和选中标签，修改通用工具仍保留原配置', async () => {
  servePlugins([plugin('bilibili', 'disabled'), plugin('qqbot', 'failed')]);
  const change = vi.fn();
  render(<AgentToolSelects token="offline" tools={['bilibili_reply_comment', 'memory']}
    mcpIds={[]} onToolsChange={change} onMcpsChange={() => {}} />);
  await waitFor(() => expect(screen.getByRole('combobox', { name: '关联 MCP（最多 3 个）' })).toBeEnabled());
  fireEvent.mouseDown(screen.getByRole('combobox', { name: '允许工具' }));
  expect(screen.queryAllByText(/B站回复评论|bilibili_reply_comment/)).toHaveLength(0);
  expect(screen.queryAllByText(/标签记忆|memory/)).toHaveLength(0);
  expect(screen.queryByText('B站我的视频 · bilibili_get_my_videos')).not.toBeInTheDocument();
  expect(screen.queryByText('B站发布图文动态 · bilibili_publish_dynamic')).not.toBeInTheDocument();
  fireEvent.click(await screen.findByText('网页读取 · read_url'));
  expect(change).toHaveBeenCalledWith(['bilibili_reply_comment', 'memory', 'read_url']);
});

it('只展示运行插件的工具，保存待启用状态不等于已经运行', async () => {
  servePlugins([plugin('bilibili', 'running'), plugin('qqbot', 'prepared')]);
  render(<AgentToolSelects token="offline" tools={[]} mcpIds={[]}
    onToolsChange={() => {}} onMcpsChange={() => {}} />);
  await waitFor(() => expect(screen.getByRole('combobox', { name: '关联 MCP（最多 3 个）' })).toBeEnabled());
  fireEvent.mouseDown(screen.getByRole('combobox', { name: '允许工具' }));
  await waitFor(() => expect(screen.getByText('B站视频投稿 · bilibili_publish_video')).toBeVisible());
  expect(screen.queryByText(/标签记忆/)).not.toBeInTheDocument();
});

it('B站未运行时不显示专属投稿视频上传入口', async () => {
  servePlugins([plugin('bilibili', 'disabled')]);
  render(<AgentChat token="offline" />);
  await waitFor(() => expect(screen.getByRole('combobox', { name: '选择 Agent' })).toBeVisible());
  expect(screen.queryByLabelText('添加投稿视频')).not.toBeInTheDocument();
});
