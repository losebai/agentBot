import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { Agents } from './Agents';
import { useRunningPluginFixture } from '../test-plugins';

useRunningPluginFixture();

it('整页直接展示首个 Agent 的角色和性格，上方按钮切换且不读取 Skill 目录', async () => {
  const fetcher = vi.fn(async (url) => Response.json({
    items: String(url).includes('/agents?') ? [{
      id: 1, revision: 2, name: '客服', code: 'support', description: '', enabled: true,
      model_id: 1, system_prompt: '解答问题', personality_prompt: '耐心',
      skill_ids: [7], tools: [], mcp_ids: [], options: {},
    }] : [], next_cursor: null,
  }));
  vi.stubGlobal('fetch', fetcher);
  render(<Agents token="offline"/>);
  expect(await screen.findByLabelText('角色')).toHaveValue('解答问题');
  expect(screen.getByLabelText('性格')).toHaveValue('耐心');
  const switches = screen.getByRole('group', { name: '切换 Agent' });
  expect(within(switches).getByRole('button', { name: '客服' })).toHaveAttribute('aria-pressed', 'true');
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(fetcher.mock.calls.some(([url]) => String(url).includes('/skills'))).toBe(false);
});

it.each(['save', 'delete'])('目录刷新晚于 %s 返回时不能覆盖写入结果', async action => {
  const agent = { id: 1, revision: 1, name: '客服', code: 'support', description: '',
    enabled: false, model_id: 1, system_prompt: '原角色', personality_prompt: '',
    skill_ids: [7], tools: [], mcp_ids: [], options: {} };
  let reads = 0;
  let resolveRead: ((response: Response) => void) | undefined;
  vi.stubGlobal('fetch', vi.fn(async (url, options) => {
    if (options?.method === 'PATCH') return Response.json({ ...agent, revision: 2, system_prompt: '已保存角色' });
    if (options?.method === 'DELETE') return new Response(null, {status: 204});
    if (String(url).includes('/agents?')) {
      if (++reads === 1) return Response.json({items: [agent], next_cursor: null});
      // 模拟已在服务端读取旧快照且忽略取消的网络响应。
      return new Promise<Response>(resolve => { resolveRead = resolve; });
    }
    return Response.json({items: [], next_cursor: null});
  }));
  render(<Agents token="offline"/>);
  await screen.findByLabelText('角色');
  fireEvent.change(screen.getByLabelText('角色'), {target: {value: '已保存角色'}});
  fireEvent.click(screen.getByRole('button', {name: /^刷\s*新$/}));
  await waitFor(() => expect(resolveRead).toBeDefined());
  if (action === 'save') fireEvent.click(screen.getByRole('button', {name: '保存 Agent'}));
  else {
    fireEvent.click(screen.getByRole('button', {name: '删除 Agent'}));
    fireEvent.click(screen.getByRole('button', {name: '确认删除'}));
  }
  await screen.findByText(action === 'save' ? 'Agent 已保存。' : 'Agent 已删除。');
  await act(async () => { resolveRead!(Response.json({items: [agent], next_cursor: null})); });
  if (action === 'save') expect(screen.getByLabelText('角色')).toHaveValue('已保存角色');
  else expect(screen.queryByRole('button', {name: '客服'})).not.toBeInTheDocument();
});

it('刷新分页外 Agent 的详情期间禁止切换，晚到详情不会使当前编辑区消失', async () => {
  const make = (id: number, name: string) => ({id, name, revision: 1, code: `agent_${id}`,
    description: '', enabled: false, model_id: 1, system_prompt: name, personality_prompt: '',
    skill_ids: [], tools: [], mcp_ids: [], options: {}});
  const first = make(1, '首页助手');
  const second = make(2, '次页助手');
  const third = make(3, '末页助手');
  let resolveDetail: ((response: Response) => void) | undefined;
  vi.stubGlobal('fetch', vi.fn(async url => {
    const path = String(url);
    if (path.endsWith('/agents/2')) return new Promise<Response>(resolve => { resolveDetail = resolve; });
    if (path.includes('/agents?')) return Response.json(path.includes('cursor=more')
      ? {items: [second, third], next_cursor: null} : {items: [first], next_cursor: 'more'});
    return Response.json({items: [], next_cursor: null});
  }));
  render(<Agents token="offline"/>);
  await screen.findByLabelText('角色');
  fireEvent.click(screen.getByRole('button', {name: '加载更多 Agent'}));
  fireEvent.click(await screen.findByRole('button', {name: '次页助手'}));
  expect(screen.getByLabelText('角色')).toHaveValue('次页助手');
  fireEvent.click(screen.getByRole('button', {name: /^刷\s*新$/}));
  await waitFor(() => expect(resolveDetail).toBeDefined());
  expect(screen.getByRole('button', {name: '末页助手'})).toBeDisabled();
  fireEvent.click(screen.getByRole('button', {name: '末页助手'}));
  await act(async () => { resolveDetail!(Response.json(second)); });
  expect(screen.getByLabelText('角色')).toHaveValue('次页助手');
  expect(screen.getByRole('button', {name: '次页助手'})).toHaveAttribute('aria-pressed', 'true');
});
