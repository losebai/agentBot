import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import type { Agent } from '../agent-types';
import { chooseSelect } from '../test-select';
import { useRunningPluginFixture } from '../test-plugins';
import { Agents } from './Agents';

useRunningPluginFixture();

/** 离线模拟目录持久化，验证多个 Agent 的草稿和实际提交内容。 */
function directory(items: Agent[], failFirst = false) {
  const writes: { url: string; body: Record<string, unknown> }[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    if (init?.method === 'POST' || init?.method === 'PATCH') {
      const body = JSON.parse(String(init.body));
      writes.push({ url: String(url), body });
      if (failFirst && writes.length === 1) {
        return Response.json({ message: '配置版本冲突', code: 'AI_CONFIG_CONFLICT' }, { status: 409 });
      }
      if (init.method === 'POST') {
        const created = { ...body, id: 3, revision: 1 } as unknown as Agent;
        items.push(created);
        return Response.json(created);
      }
      const index = items.findIndex(item => String(url).endsWith(`/agents/${item.id}`));
      items[index] = { ...items[index], ...body, revision: items[index].revision + 1 };
      return Response.json(items[index]);
    }
    if (String(url).includes('/ai/models?')) return Response.json({ items: [
      { id: 1, reference: 'model:1', provider_id: 1, provider_name: '离线供应商',
        upstream_model: 'chat', alias: 'chat', revision: 1, enabled: true },
    ], next_cursor: null });
    return Response.json({ items: String(url).includes('/agents?') ? items : [], next_cursor: null });
  }));
  return writes;
}

/** 保留旧接口缺失人格字段的夹具，用于验证打开编辑时的兼容行为。 */
function legacyAgent(): Agent {
  return { id: 1, revision: 4, name: '旧助手', code: 'legacy', description: '', enabled: true,
    model_id: 1, vision_model_id: null, fallback_model_id: null,
    system_prompt: '  原角色\n', tools: [], skill_ids: [], mcp_ids: [], options: {} };
}

it('新增 Agent 分别保存角色与性格，预览反映尚未保存的两段文本', async () => {
  const writes = directory([]);
  render(<Agents token="offline" />);
  await waitFor(() => expect(screen.getByRole('button', { name: '新增 Agent' })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: '新增 Agent' }));
  fireEvent.change(screen.getByLabelText('名称'), { target: { value: '讲师' } });
  fireEvent.change(screen.getByLabelText('Agent 代码'), { target: { value: 'teacher' } });
  await waitFor(() => expect(screen.getByRole('combobox', { name: '聊天模型' })).toBeEnabled());
  await chooseSelect(screen.getByRole('combobox', { name: '聊天模型' }), '离线供应商 / chat');
  fireEvent.change(screen.getByLabelText('角色'), { target: { value: '  负责教学  ' } });
  fireEvent.change(screen.getByLabelText('性格'), { target: { value: '  耐心温和\n' } });
  expect(screen.getByLabelText('组合提示词预览').textContent).toBe('【角色设定】\n负责教学\n\n【人格设定】\n耐心温和');
  expect(screen.getByText('工具调用次数在“机器人配置 → LLM设置”中统一调整。')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '保存 Agent' }));
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  await waitFor(() => expect(writes).toHaveLength(1));
  expect(screen.getByLabelText('性格')).toHaveValue('  耐心温和\n');
  expect(writes[0].body).toMatchObject({ name: '讲师', code: 'teacher', system_prompt: '  负责教学  ', personality_prompt: '  耐心温和\n' });
});

it('旧 Agent 的空性格保持角色原文，空角色只预览人格段且撤销丢弃两项草稿', async () => {
  const writes = directory([legacyAgent()]);
  render(<Agents token="offline" />);
  await screen.findByLabelText('角色');
  expect(screen.getByLabelText('性格')).toHaveValue('');
  expect(screen.getByLabelText('组合提示词预览').textContent).toBe('  原角色\n');
  fireEvent.change(screen.getByLabelText('性格'), { target: { value: ' \n ' } });
  expect(screen.getByLabelText('组合提示词预览').textContent).toBe('  原角色\n');
  fireEvent.change(screen.getByLabelText('角色'), { target: { value: ' \n ' } });
  fireEvent.change(screen.getByLabelText('性格'), { target: { value: ' 活泼 ' } });
  expect(screen.getByLabelText('组合提示词预览').textContent).toBe('【人格设定】\n活泼');
  fireEvent.click(screen.getByRole('button', { name: '撤销修改' }));
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  fireEvent.click(within(screen.getByRole('group', { name: '切换 Agent' })).getByRole('button', { name: '旧助手' }));
  expect(screen.getByLabelText('角色')).toHaveValue('  原角色\n');
  expect(screen.getByLabelText('性格')).toHaveValue('');
  fireEvent.click(screen.getByRole('button', { name: '保存 Agent' }));
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(writes).toEqual([]);
});

it('多个 Agent 独立保存两项设置，清空性格恢复原角色预览', async () => {
  const writes = directory([legacyAgent(), { ...legacyAgent(), id: 2, revision: 8, name: '新助手',
    code: 'new_agent', system_prompt: '新角色', personality_prompt: '严谨' }]);
  render(<Agents token="offline" />);
  await screen.findByLabelText('角色');
  fireEvent.change(screen.getByLabelText('角色'), { target: { value: '旧角色已更新' } });
  fireEvent.change(screen.getByLabelText('性格'), { target: { value: '温和' } });
  fireEvent.click(screen.getByRole('button', { name: '保存 Agent' }));
  await screen.findByText('Agent 已保存。');
  expect(screen.getByLabelText('角色')).toHaveValue('旧角色已更新');
  fireEvent.click(within(screen.getByRole('group', { name: '切换 Agent' })).getByRole('button', { name: '新助手' }));
  expect(screen.getByLabelText('角色')).toHaveValue('新角色');
  expect(screen.getByLabelText('性格')).toHaveValue('严谨');
  fireEvent.change(screen.getByLabelText('性格'), { target: { value: '' } });
  expect(screen.getByLabelText('组合提示词预览').textContent).toBe('新角色');
  fireEvent.click(screen.getByRole('button', { name: '保存 Agent' }));
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  await waitFor(() => expect(writes).toHaveLength(2));
  expect(writes).toEqual([
    { url: '/api/v1/agents/1', body: { expected_revision: 4, system_prompt: '旧角色已更新', personality_prompt: '温和' } },
    { url: '/api/v1/agents/2', body: { expected_revision: 8, personality_prompt: '' } },
  ]);
  fireEvent.click(within(screen.getByRole('group', { name: '切换 Agent' })).getByRole('button', { name: '旧助手' }));
  expect(screen.getByLabelText('性格')).toHaveValue('温和');
});

it('保存冲突保留角色与性格草稿，重试继续使用原版本', async () => {
  const writes = directory([legacyAgent()], true);
  render(<Agents token="offline" />);
  await screen.findByLabelText('角色');
  fireEvent.change(screen.getByLabelText('角色'), { target: { value: '新角色' } });
  fireEvent.change(screen.getByLabelText('性格'), { target: { value: '新人格' } });
  fireEvent.click(screen.getByRole('button', { name: '保存 Agent' }));
  await waitFor(() => expect(screen.getByText('配置版本冲突')).toBeVisible());
  expect(screen.getByRole('group', { name: '操作错误' })).toHaveFocus();
  expect(screen.getByLabelText('角色')).toHaveValue('新角色');
  expect(screen.getByLabelText('性格')).toHaveValue('新人格');
  fireEvent.click(screen.getByRole('button', { name: '保存 Agent' }));
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  await waitFor(() => expect(writes).toHaveLength(2));
  expect(writes.map(write => write.body)).toEqual([
    { expected_revision: 4, system_prompt: '新角色', personality_prompt: '新人格' },
    { expected_revision: 4, system_prompt: '新角色', personality_prompt: '新人格' },
  ]);
});

it('组合超过 32000 字符时明确拒绝保存，保留全部输入并计入分段标题', async () => {
  const writes = directory([legacyAgent()]);
  render(<Agents token="offline" />);
  await screen.findByLabelText('角色');
  fireEvent.change(screen.getByLabelText('角色'), { target: { value: '角'.repeat(31999) } });
  fireEvent.change(screen.getByLabelText('性格'), { target: { value: '人' } });
  await waitFor(() => expect(screen.getByText(/组合提示词不能超过 32000 字符/)).toBeVisible());
  expect(screen.getByRole('button', { name: '保存 Agent' })).toBeDisabled();
  expect(screen.getByLabelText('角色')).toHaveValue('角'.repeat(31999));
  fireEvent.submit(screen.getByRole('button', { name: '保存 Agent' }).closest('form')!);
  expect(writes).toEqual([]);
  fireEvent.change(screen.getByLabelText('角色'), { target: { value: '😀'.repeat(32000) } });
  fireEvent.change(screen.getByLabelText('性格'), { target: { value: '' } });
  expect(screen.getByRole('button', { name: '保存 Agent' })).toBeEnabled();
  fireEvent.click(screen.getByRole('button', { name: '保存 Agent' }));
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  await waitFor(() => expect(writes).toHaveLength(1));
  expect(writes[0].body.system_prompt).toBe('😀'.repeat(32000));
});

it('原始角色与性格各自超限时也明确阻止保存，不因组合时去空白而放行', async () => {
  const writes = directory([legacyAgent()]);
  render(<Agents token="offline" />);
  await screen.findByLabelText('角色');
  fireEvent.change(screen.getByLabelText('性格'), { target: { value: ' '.repeat(32001) } });
  await waitFor(() => expect(screen.getByText(/性格不能超过 32000 字符/)).toBeVisible());
  expect(screen.getByRole('button', { name: '保存 Agent' })).toBeDisabled();
  expect(screen.getByLabelText('性格')).toHaveValue(' '.repeat(32001));
  fireEvent.change(screen.getByLabelText('性格'), { target: { value: '耐心' } });
  fireEvent.change(screen.getByLabelText('角色'), { target: { value: ' '.repeat(32001) } });
  expect(screen.getByText(/角色不能超过 32000 字符/)).toBeVisible();
  expect(screen.getByRole('button', { name: '保存 Agent' })).toBeDisabled();
  fireEvent.submit(screen.getByRole('button', { name: '保存 Agent' }).closest('form')!);
  expect(writes).toEqual([]);
});

it('切换 Agent 保留各自未保存的角色与性格，保存使用各自的版本与权限', async () => {
  const writes = directory([
    { ...legacyAgent(), skill_ids: [41], mcp_ids: [51], tools: ['memory'] },
    { ...legacyAgent(), id: 2, revision: 8, name: '新助手', code: 'second',
      system_prompt: '第二角色', personality_prompt: '严谨', skill_ids: [42], mcp_ids: [52] },
  ]);
  render(<Agents token="offline" />);
  await screen.findByLabelText('角色');
  const switches = screen.getByRole('group', { name: '切换 Agent' });
  expect(within(switches).getByRole('button', { name: '旧助手' })).toHaveAttribute('aria-pressed', 'true');
  fireEvent.change(screen.getByLabelText('角色'), { target: { value: '第一草稿' } });
  fireEvent.change(screen.getByLabelText('性格'), { target: { value: '温和' } });
  fireEvent.click(within(switches).getByRole('button', { name: '新助手' }));
  expect(screen.getByLabelText('角色')).toHaveValue('第二角色');
  expect(screen.getByLabelText('性格')).toHaveValue('严谨');
  fireEvent.change(screen.getByLabelText('性格'), { target: { value: '第二草稿' } });
  fireEvent.click(within(switches).getByRole('button', { name: '旧助手' }));
  expect(screen.getByLabelText('角色')).toHaveValue('第一草稿');
  expect(screen.getByLabelText('性格')).toHaveValue('温和');
  fireEvent.click(screen.getByRole('button', { name: '保存 Agent' }));
  await screen.findByText('Agent 已保存。');
  fireEvent.click(within(switches).getByRole('button', { name: '新助手' }));
  expect(screen.getByLabelText('性格')).toHaveValue('第二草稿');
  fireEvent.click(screen.getByRole('button', { name: '保存 Agent' }));
  await screen.findByText('Agent 已保存。');
  // 提示词修改仅提交差异，隐藏的 Skill、远程服务及工具权限不得清空。
  expect(writes.map(write => write.body)).toEqual([
    { expected_revision: 4, system_prompt: '第一草稿', personality_prompt: '温和' },
    { expected_revision: 8, personality_prompt: '第二草稿' },
  ]);
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});

it('新建草稿切换后仍保留，取消新建只丢弃新建内容', async () => {
  const writes = directory([legacyAgent()]);
  render(<Agents token="offline" />);
  await screen.findByLabelText('角色');
  fireEvent.change(screen.getByLabelText('角色'), { target: { value: '已有 Agent 草稿' } });
  fireEvent.click(screen.getByRole('button', { name: '新增 Agent' }));
  expect(screen.getByLabelText('角色')).toHaveValue('');
  expect(screen.getByLabelText('性格')).toHaveValue('');
  fireEvent.change(screen.getByLabelText('名称'), { target: { value: '新建助手' } });
  fireEvent.change(screen.getByLabelText('Agent 代码'), { target: { value: 'new_agent' } });
  fireEvent.change(screen.getByLabelText('角色'), { target: { value: '新建角色草稿' } });
  fireEvent.change(screen.getByLabelText('性格'), { target: { value: '新建性格草稿' } });
  const switches = screen.getByRole('group', { name: '切换 Agent' });
  fireEvent.click(within(switches).getByRole('button', { name: '旧助手' }));
  expect(screen.getByLabelText('角色')).toHaveValue('已有 Agent 草稿');
  fireEvent.click(within(switches).getByRole('button', { name: '新建助手' }));
  expect(screen.getByLabelText('Agent 代码')).toHaveValue('new_agent');
  expect(screen.getByLabelText('角色')).toHaveValue('新建角色草稿');
  expect(screen.getByLabelText('性格')).toHaveValue('新建性格草稿');
  fireEvent.click(screen.getByRole('button', { name: '取消新建' }));
  expect(within(switches).queryByRole('button', { name: '新建助手' })).not.toBeInTheDocument();
  expect(screen.getByLabelText('角色')).toHaveValue('已有 Agent 草稿');
  expect(writes).toEqual([]);
});
