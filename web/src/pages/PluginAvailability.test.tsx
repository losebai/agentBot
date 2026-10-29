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
  await waitFor(() => expect(screen.getByRole('combobox', { name: '关联远程服务（最多 3 个）' })).toBeEnabled());
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
  await waitFor(() => expect(screen.getByRole('combobox', { name: '关联远程服务（最多 3 个）' })).toBeEnabled());
  fireEvent.mouseDown(screen.getByRole('combobox', { name: '允许工具' }));
  await waitFor(() => expect(screen.getByText('B站视频投稿 · bilibili_publish_video')).toBeVisible());
  expect(screen.queryByText(/标签记忆/)).not.toBeInTheDocument();
});

it('B站未运行时统一文件入口不开放视频类型', async () => {
  servePlugins([plugin('bilibili', 'disabled')]);
  render(<AgentChat token="offline" />);
  await waitFor(() => expect(screen.getByRole('combobox', { name: '选择 Agent' })).toBeVisible());
  expect(screen.queryByLabelText('添加投稿视频')).not.toBeInTheDocument();
  expect(screen.getByLabelText('添加文件')).not.toHaveAttribute('accept', expect.stringContaining('video/mp4'));
});

it.each(['disabled', 'prepared', 'failed', 'stopped'] as const)('表情包插件 %s 时不能访问图库，隐藏工具仍保留配置', async status => {
  servePlugins([plugin('stickers', status)]);
  const view = render(<PluginConfiguration token="offline" pluginId="stickers" />);
  expect(await screen.findByText('插件未运行，请在插件中心启用并手动重启后端。')).toBeVisible();
  expect(screen.queryByRole('region', { name: '表情包图库' })).not.toBeInTheDocument();
  view.unmount();
  const changed = vi.fn();
  render(<AgentToolSelects token="offline" tools={['sticker_search', 'sticker_send']} mcpIds={[]}
    onToolsChange={changed} onMcpsChange={() => {}} />);
  await waitFor(() => expect(screen.getByRole('combobox', { name: '关联远程服务（最多 3 个）' })).toBeEnabled());
  fireEvent.mouseDown(screen.getByRole('combobox', { name: '允许工具' }));
  expect(screen.queryByText('表情包检索 · sticker_search')).not.toBeInTheDocument();
  expect(screen.queryByText('表情包发送 · sticker_send')).not.toBeInTheDocument();
  fireEvent.click(await screen.findByText('网页读取 · read_url'));
  expect(changed).toHaveBeenCalledWith(['sticker_search', 'sticker_send', 'read_url']);
});

it('只有实际运行的表情包插件开放模型配置分类与两项工具', async () => {
  servePlugins([plugin('stickers', 'running')]);
  const view = render(<Settings token="offline" />);
  expect(await screen.findByRole('button', { name: '表情包设置' })).toBeVisible();
  view.unmount();
  render(<AgentToolSelects token="offline" tools={[]} mcpIds={[]} onToolsChange={() => {}} onMcpsChange={() => {}} />);
  await waitFor(() => expect(screen.getByRole('combobox', { name: '关联远程服务（最多 3 个）' })).toBeEnabled());
  fireEvent.mouseDown(screen.getByRole('combobox', { name: '允许工具' }));
  await waitFor(() => expect(screen.getByText('表情包检索 · sticker_search')).toBeVisible());
  expect(screen.getByText('表情包发送 · sticker_send')).toBeVisible();
});

it('表情包默认显示列表，配置独立成Tab且切换保留草稿，保存仅提交编辑配置', async () => {
  let submitted: unknown;
  vi.stubGlobal('fetch', vi.fn(async (input, init) => {
    const path = String(input);
    if (path.endsWith('/plugins')) return Response.json([plugin('stickers', 'running')]);
    if (path.endsWith('/settings')) {
      if (init?.method === 'PATCH') submitted = JSON.parse(String(init.body));
      return Response.json({ database_revision: 7, applied_revision: 7, startup_revision: null, restart_required_fields: [], fields: [
        { name: 'chat_sticker_model', description: '表情包识图模型', value: '', kind: 'string', nullable: false },
        { name: 'chat_sticker_vision_prompt', description: '表情包识图要求', value: '', kind: 'string', nullable: false },
        { name: 'chat_sticker_timeout_seconds', description: '表情包超时', value: 60, kind: 'number', nullable: false },
        { name: 'chat_model', description: '普通聊天模型', value: 'model:1', kind: 'string', nullable: false },
      ] });
    }
    if (path.includes('/stickers?')) return Response.json({ items: [], total: 0, next_offset: null });
    return Response.json({ items: [], next_cursor: null });
  }));
  render(<PluginConfiguration token="offline" pluginId="stickers" />);
  expect(await screen.findByRole('region', { name: '表情包图库' })).toBeVisible();
  expect(screen.queryByLabelText('表情包识图要求')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('tab', { name: '配置' }));
  const prompt = await screen.findByLabelText('表情包识图要求');
  expect(prompt.tagName).toBe('TEXTAREA');
  expect(screen.getByLabelText('表情包超时')).toHaveValue(60);
  expect(screen.getByRole('combobox', { name: '表情包识图模型' })).toBeInTheDocument();
  expect(screen.queryByLabelText('普通聊天模型')).not.toBeInTheDocument();
  expect(screen.queryByRole('region', { name: '表情包图库' })).not.toBeInTheDocument();
  fireEvent.change(prompt, { target: { value: '说明情绪和适用场景' } });
  fireEvent.click(screen.getByRole('tab', { name: '列表' }));
  expect(await screen.findByRole('region', { name: '表情包图库' })).toBeVisible();
  fireEvent.click(screen.getByRole('tab', { name: '配置' }));
  expect(screen.getByLabelText('表情包识图要求')).toHaveValue('说明情绪和适用场景');
  fireEvent.click(screen.getByRole('button', { name: '保存修改' }));
  await waitFor(() => expect(submitted).toEqual({ expected_revision: 7, values: { chat_sticker_vision_prompt: '说明情绪和适用场景' } }));
});
