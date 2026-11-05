import { useRunningPluginFixture } from '../test-plugins';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { chooseSelect, selectDisplay } from '../test-select';
import { Settings } from './Settings';

useRunningPluginFixture();

/** 提供独立模式与决策字段，捕获页面实际提交值，避免依赖后端服务。 */
function mockAutoSpeakSettings(mode: string | null = 'interval') {
  const fields = [
    { name: 'chat_auto_speak_trigger_mode', description: '自动发言触发模式', kind: 'string', value: mode ?? undefined },
    { name: 'chat_auto_speak_interval_seconds', description: '定时检查间隔', kind: 'number', value: 3600 },
    { name: 'chat_auto_speak_initial_delay_seconds', description: '定时首次延迟', kind: 'number', value: 60 },
    { name: 'chat_auto_speak_message_interval_seconds', description: '消息检查间隔', kind: 'number', value: 30 },
    { name: 'chat_auto_speak_decision_backend', description: '自动发言决策方式', kind: 'string', value: 'laya' },
    { name: 'chat_auto_speak_mcp_code', description: '自动发言 MCP', kind: 'string', value: 'decision' },
    { name: 'chat_auto_speak_llm_prompt', description: '自动发言 LLM 决策提示词', kind: 'string', value: '独立指令' },
    { name: 'chat_auto_speak_prompt', description: '自动发言补充规则', kind: 'string', value: '共用规则' },
  ].filter(field => field.value !== undefined).map(field => ({ ...field,
    nullable: false, secret_is_set: false, restart_required: false,
  }));
  const snapshot = { database_revision: 4, applied_revision: 4, startup_revision: null,
    restart_required_fields: [], fields };
  const request = { submitted: undefined as unknown };
  vi.stubGlobal('fetch', vi.fn(async (_url, init) => {
    if (init?.method === 'PATCH') request.submitted = JSON.parse(String(init.body));
    return Response.json(snapshot);
  }));
  return request;
}

it('触发模式立即更换时间字段，往返保留草稿且与决策方式独立', async () => {
  const request = mockAutoSpeakSettings();
  render(<Settings token="offline" />);
  fireEvent.click(await screen.findByRole('button', { name: '自动回复设置' }));
  const mode = await screen.findByLabelText('自动发言触发模式');
  expect(selectDisplay(mode)).toHaveTextContent('定时模式');
  expect(screen.queryByLabelText('消息检查间隔')).not.toBeInTheDocument();
  fireEvent.change(screen.getByLabelText('定时检查间隔'), { target: { value: '1800' } });
  fireEvent.change(screen.getByLabelText('定时首次延迟'), { target: { value: '90' } });
  await chooseSelect(mode, '消息模式');
  expect(screen.queryByLabelText('定时检查间隔')).not.toBeInTheDocument();
  expect(screen.queryByLabelText('定时首次延迟')).not.toBeInTheDocument();
  fireEvent.change(screen.getByLabelText('消息检查间隔'), { target: { value: '120' } });
  await chooseSelect(screen.getByLabelText('自动发言决策方式'), 'LLM');
  expect(screen.getByLabelText('自动发言 LLM 决策提示词')).toBeInTheDocument();
  expect(screen.queryByLabelText('自动发言 MCP')).not.toBeInTheDocument();
  expect(screen.getByLabelText('消息检查间隔')).toHaveValue(120);
  await chooseSelect(mode, '定时模式');
  expect(screen.getByLabelText('定时检查间隔')).toHaveValue(1800);
  expect(screen.getByLabelText('定时首次延迟')).toHaveValue(90);
  expect(screen.queryByLabelText('消息检查间隔')).not.toBeInTheDocument();
  expect(screen.getByLabelText('自动发言 LLM 决策提示词')).toBeInTheDocument();
  await chooseSelect(mode, '消息模式');
  await chooseSelect(screen.getByLabelText('自动发言决策方式'), 'Laya');
  expect(screen.getByLabelText('自动发言 MCP')).toBeInTheDocument();
  expect(screen.getByLabelText('消息检查间隔')).toHaveValue(120);
  expect(screen.getByLabelText('自动发言补充规则')).toHaveValue('共用规则');
  fireEvent.click(screen.getByRole('button', { name: '保存修改' }));
  await waitFor(() => expect(request.submitted).toEqual({ expected_revision: 4, values: {
    chat_auto_speak_trigger_mode: 'message', chat_auto_speak_interval_seconds: 1800,
    chat_auto_speak_initial_delay_seconds: 90, chat_auto_speak_message_interval_seconds: 120,
    chat_auto_speak_decision_backend: 'laya',
  } }));
}, 15000);

it('消息快照显示独立间隔，零值按数字提交且不写回未编辑定时字段', async () => {
  const request = mockAutoSpeakSettings('message');
  render(<Settings token="offline" />);
  fireEvent.click(await screen.findByRole('button', { name: '自动回复设置' }));
  expect(selectDisplay(await screen.findByLabelText('自动发言触发模式'))).toHaveTextContent('消息模式');
  expect(screen.queryByLabelText('定时检查间隔')).not.toBeInTheDocument();
  expect(screen.queryByLabelText('定时首次延迟')).not.toBeInTheDocument();
  fireEvent.change(screen.getByLabelText('消息检查间隔'), { target: { value: '0' } });
  fireEvent.click(screen.getByRole('button', { name: '保存修改' }));
  await waitFor(() => expect(request.submitted).toEqual({ expected_revision: 4,
    values: { chat_auto_speak_message_interval_seconds: 0 } }));
});

it('旧快照缺少触发模式时按定时模式展示，不自动生成新字段草稿', async () => {
  mockAutoSpeakSettings(null);
  render(<Settings token="offline" />);
  fireEvent.click(await screen.findByRole('button', { name: '自动回复设置' }));
  expect(await screen.findByLabelText('定时检查间隔')).toHaveValue(3600);
  expect(screen.getByLabelText('定时首次延迟')).toHaveValue(60);
  expect(screen.queryByLabelText('消息检查间隔')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: '保存修改' })).toBeDisabled();
});

/** 模拟统一配置 API，使用模型目录稳定引用验证决策与聊天模型独立。 */
it('自动发言二选一，LLM 独立模型和提示词保存且切换保留草稿', async () => {
  const fields = [
    ['chat_auto_speak_decision_backend', '自动发言决策方式', 'laya'],
    ['chat_auto_speak_decision_model', '自动发言决策模型', ''],
    ['chat_auto_speak_llm_prompt', '自动发言 LLM 决策提示词', '独立指令'],
    ['chat_auto_speak_mcp_code', '自动发言 MCP', 'decision'],
    ['chat_auto_speak_laya_prompt', '自动发言决策指令', 'Laya 指令'],
    ['chat_model', '聊天模型', 'model:1'],
  ].map(([name, description, value]) => ({
    name, description, value, kind: 'string', nullable: false,
    secret_is_set: false, restart_required: false,
  }));
  const snapshot = { database_revision: 4, applied_revision: 4, startup_revision: null,
    restart_required_fields: [], fields };
  let submitted: unknown;
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    if (String(url).includes('/ai/models?')) return Response.json({ items: [{
      id: 7, reference: 'model:7', provider_id: 1, provider_name: '测试供应商',
      upstream_model: 'decision-model', enabled: true, revision: 1,
    }], next_cursor: null });
    if (init?.method === 'PATCH') submitted = JSON.parse(String(init.body));
    return Response.json(String(url).endsWith('/settings') ? snapshot
      : { available: false, text: 0, images: 0, total: 0 });
  }));
  render(<Settings token="offline" />);
  fireEvent.click(await screen.findByRole('button', { name: '自动回复设置' }));
  await screen.findByLabelText('自动发言决策方式');
  expect(screen.getByLabelText('自动发言 MCP')).toBeInTheDocument();
  expect(screen.queryByLabelText('自动发言决策模型')).not.toBeInTheDocument();
  await chooseSelect(screen.getByLabelText('自动发言决策方式'), 'LLM');
  await waitFor(() => expect(screen.getByLabelText('自动发言决策模型')).toBeEnabled());
  expect(screen.queryByLabelText('自动发言 MCP')).not.toBeInTheDocument();
  expect(screen.queryByLabelText('自动发言决策指令')).not.toBeInTheDocument();
  await chooseSelect(screen.getByLabelText('自动发言决策模型'), 'decision-model');
  fireEvent.change(screen.getByLabelText('自动发言 LLM 决策提示词'), {
    target: { value: '只判断当次资料，不接入会话历史' },
  });
  await chooseSelect(screen.getByLabelText('自动发言决策方式'), 'Laya');
  expect(screen.getByLabelText('自动发言 MCP')).toHaveValue('decision');
  await chooseSelect(screen.getByLabelText('自动发言决策方式'), 'LLM');
  await waitFor(() => expect(screen.getByLabelText('自动发言决策模型')).toBeEnabled());
  expect(selectDisplay(screen.getByLabelText('自动发言决策模型'))).toHaveTextContent('decision-model');
  expect(screen.getByLabelText('自动发言 LLM 决策提示词')).toHaveValue('只判断当次资料，不接入会话历史');
  fireEvent.click(screen.getByText('保存修改'));
  await waitFor(() => expect(submitted).toEqual({ expected_revision: 4, values: {
    chat_auto_speak_decision_backend: 'llm', chat_auto_speak_decision_model: 'model:7',
    chat_auto_speak_llm_prompt: '只判断当次资料，不接入会话历史',
  } }));
}, 15000);
