import { useRunningPluginFixture } from '../test-plugins';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { Agents } from './Agents';
import { Settings } from './Settings';
import type { SettingField, SettingsSnapshot } from '../types';

useRunningPluginFixture();

const common = { nullable: false, secret_is_set: false, restart_required: false };
const agentFields: SettingField[] = [
  { ...common, name: 'chat_python_agent_code', description: '默认 Agent 预设', kind: 'string', value: 'helper', nullable: true },
  { ...common, name: 'chat_agent_max_steps', description: 'Agent 工具调用次数', kind: 'number', value: 5, nullable: true },
  { ...common, name: 'chat_mcp_services', description: '通用 MCP 服务', kind: 'json', value: ['memory'] },
  { ...common, name: 'chat_web_search_enabled', description: '联网搜索授权', kind: 'boolean', value: true },
  { ...common, name: 'chat_web_search_max_steps', description: '联网推理轮次', kind: 'number', value: 3 },
  { ...common, name: 'chat_mention_tool_enabled', description: '群成员提及工具授权', kind: 'boolean', value: false },
];
const snapshot: SettingsSnapshot = { database_revision: 4, applied_revision: 4, restart_required_fields: [], startup_revision: null, fields: [
  ...agentFields,
  { ...common, name: 'chat_model', description: '聊天模型', kind: 'string', value: 'local-model' },
  { ...common, name: 'port', description: '监听端口', kind: 'number', value: 8080, scope: 'static', read_only: true },
] };

it('机器人配置将模型和 Agent 归入 LLM，提及授权归入 QQ，端口在基础设置只读', async () => {
  vi.stubGlobal('fetch', vi.fn(async url => Response.json(String(url).includes('/ai/models?')
    ? { items: [], next_cursor: null } : snapshot)));
  render(<Settings token="offline" />);
  expect(await screen.findByLabelText('监听端口')).toBeDisabled();
  for (const field of agentFields) expect(screen.queryByLabelText(field.description)).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'LLM设置' }));
  expect(screen.getByLabelText('聊天模型')).toBeVisible();
  expect(screen.queryByLabelText('搜索 MCP 服务')).not.toBeInTheDocument();
  for (const field of agentFields.filter(field => !['chat_mention_tool_enabled', 'chat_web_search_max_steps'].includes(field.name))) {
    expect(screen.getByLabelText(field.description)).toBeVisible();
  }
  fireEvent.click(await screen.findByRole('button', { name: 'QQ' }));
  expect(screen.getByLabelText('群成员提及工具授权')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '基础设置' }));
  expect(await screen.findByLabelText('监听端口')).toBeDisabled();
});

it('在机器人配置修改 Agent 运行字段，保存保留原有版本边界', async () => {
  let submitted: unknown;
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    if (String(url).endsWith('/settings')) {
      if (init?.method === 'PATCH') {
        submitted = JSON.parse(String(init.body));
        return Response.json({ ...snapshot, database_revision: 5 });
      }
      return Response.json(snapshot);
    }
    return Response.json({ items: [], next_cursor: null });
  }));
  render(<Settings token="offline" />);
  fireEvent.click(screen.getByRole('button', { name: 'LLM设置' }));
  expect(await screen.findByLabelText('默认 Agent 预设')).toBeVisible();
  expect(screen.queryByLabelText('启用 QQ Agent')).not.toBeInTheDocument();
  expect(screen.queryByLabelText('搜索 MCP 服务')).not.toBeInTheDocument();
  for (const field of agentFields.filter(field => !['chat_mention_tool_enabled', 'chat_web_search_max_steps'].includes(field.name))) {
    expect(screen.getByLabelText(field.description)).toBeVisible();
  }
  expect(screen.getByLabelText('聊天模型')).toBeVisible();
  fireEvent.change(screen.getByLabelText('默认 Agent 预设'), { target: { value: '' } });
  fireEvent.change(screen.getByLabelText('Agent 工具调用次数'), { target: { value: '50' } });
  fireEvent.change(screen.getByLabelText('通用 MCP 服务'), { target: { value: '["memory", "tools"]' } });
  fireEvent.click(screen.getByRole('button', { name: '保存修改' }));
  await waitFor(() => expect(submitted).toEqual({ expected_revision: 4, values: {
    chat_python_agent_code: null, chat_agent_max_steps: 50,
    chat_mcp_services: ['memory', 'tools'],
  } }));
  expect(await screen.findByText(/数据库版本 5/)).toBeVisible();
  expect(screen.getByText('等待热刷新')).toBeVisible();
});

it('切换机器人配置分类保留 Agent 草稿，保存冲突仍保留输入', async () => {
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    if (String(url).endsWith('/settings')) return init?.method === 'PATCH'
      ? Response.json({ code: 'SETTINGS_CONFLICT', message: '配置已被修改，请重新加载' }, { status: 409 })
      : Response.json(snapshot);
    return Response.json({ items: [], next_cursor: null });
  }));
  render(<Settings token="offline" />);
  fireEvent.click(screen.getByRole('button', { name: 'LLM设置' }));
  fireEvent.change(await screen.findByLabelText('默认 Agent 预设'), { target: { value: 'unsaved-agent' } });
  fireEvent.click(await screen.findByRole('button', { name: 'QQ' }));
  expect(screen.getByLabelText('群成员提及工具授权')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: 'LLM设置' }));
  expect(screen.getByLabelText('默认 Agent 预设')).toHaveValue('unsaved-agent');
  fireEvent.click(screen.getByRole('button', { name: '保存修改' }));
  expect(await screen.findByText('配置已被修改，请重新加载')).toBeVisible();
  expect(screen.getByLabelText('默认 Agent 预设')).toHaveValue('unsaved-agent');
});

it('Agent 管理只提供预设编辑，不再重复展示运行配置', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => Response.json({ items: [], next_cursor: null })));
  render(<Agents token="offline" />);
  await waitFor(() => expect(screen.getByRole('button', { name: '新增 Agent' })).toBeEnabled());
  expect(screen.queryByRole('tab', { name: '运行配置' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '新增 Agent' }));
  expect(screen.queryByLabelText('工具轮次')).not.toBeInTheDocument();
  // 此处验证预算入口归属，弹窗动画的临时隐藏状态不影响表单内容断言。
  expect(screen.getByText('工具调用次数在“机器人配置 → LLM设置”中统一调整。')).toBeInTheDocument();
});

it('清空工具调用次数提交 null，旧联网轮次不再展示', async () => {
  let submitted: unknown;
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    if (String(url).endsWith('/settings')) {
      if (init?.method === 'PATCH') submitted = JSON.parse(String(init.body));
      return Response.json(snapshot);
    }
    return Response.json({ items: [], next_cursor: null });
  }));
  render(<Settings token="offline" />);
  fireEvent.click(screen.getByRole('button', { name: 'LLM设置' }));
  fireEvent.change(await screen.findByLabelText('Agent 工具调用次数'), { target: { value: '' } });
  expect(screen.queryByLabelText('联网推理轮次')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '保存修改' }));
  await waitFor(() => expect(submitted).toEqual({ expected_revision: 4, values: {
    chat_agent_max_steps: null,
  } }));
});
