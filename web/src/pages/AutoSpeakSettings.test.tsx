import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { chooseSelect, selectDisplay } from '../test-select';
import { Settings } from './Settings';

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
  fireEvent.click(screen.getByRole('button', { name: '自动回复设置' }));
  await screen.findByLabelText('自动发言决策方式');
  expect(screen.getByLabelText('自动发言 MCP')).toBeInTheDocument();
  expect(screen.queryByLabelText('自动发言决策模型')).not.toBeInTheDocument();
  await chooseSelect(screen.getByLabelText('自动发言决策方式'), 'LLM');
  await waitFor(() => expect(screen.getByLabelText('自动发言决策模型')).toBeEnabled());
  expect(screen.queryByLabelText('自动发言 MCP')).not.toBeInTheDocument();
  expect(screen.queryByLabelText('自动发言决策指令')).not.toBeInTheDocument();
  await chooseSelect(screen.getByLabelText('自动发言决策模型'), '测试供应商 / decision-model');
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
