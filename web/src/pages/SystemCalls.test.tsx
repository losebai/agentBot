import { chooseSelect } from '../test-select';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { Sessions } from './Sessions';

it('系统调用独立筛选并分页，返回普通调用后清空系统筛选和页码', async () => {
  const requests: URL[] = [];
  vi.stubGlobal('fetch', vi.fn(async url => {
    const request = new URL(String(url), 'http://offline');
    requests.push(request);
    return Response.json(request.pathname.endsWith('/count') ? { total: 120 }
      : { items: [], next_cursor: request.searchParams.get('page') === '2' ? null : 'next' });
  }));
  render(<Sessions token="offline" />);
  await waitFor(() => expect(requests.at(-1)?.searchParams.get('category')).toBe('conversation'));
  expect(screen.queryByRole('option', { name: '自动发言决策' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('tab', { name: '系统调用' }));
  await waitFor(() => expect(requests.at(-1)?.searchParams.get('category')).toBe('system'));
  expect(screen.getByRole('heading', { name: '系统调用' })).toBeVisible();
  fireEvent.mouseDown(screen.getByRole('combobox', { name: '类型' }));
  expect(await screen.findByRole('option', { name: '自动发言决策' })).toBeInTheDocument();
  expect(screen.queryByRole('option', { name: '普通调用' })).not.toBeInTheDocument();
  fireEvent.keyDown(screen.getByRole('combobox', { name: '类型' }), { key: 'Escape' });
  await chooseSelect(screen.getByRole('combobox', { name: '类型' }), '指令调用');
  await waitFor(() => expect(screen.getByRole('button', { name: /筛\s*选/ })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: /筛\s*选/ }));
  await waitFor(() => expect(requests.at(-1)?.searchParams.get('system_kind')).toBe('command'));
  await waitFor(() => expect(screen.getByRole('button', { name: '下一页' })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: '下一页' }));
  await waitFor(() => expect(requests.at(-1)?.searchParams.get('page')).toBe('2'));
  expect(requests.at(-1)?.searchParams.get('category')).toBe('system');
  fireEvent.click(screen.getByRole('tab', { name: '调用记录' }));
  await waitFor(() => expect(requests.at(-1)?.searchParams.get('category')).toBe('conversation'));
  expect(requests.at(-1)?.searchParams.has('system_kind')).toBe(false);
  expect(requests.at(-1)?.searchParams.has('cursor')).toBe(false);
  expect(requests.at(-1)?.searchParams.get('page')).toBe('1');
});

it('系统指令仍可查看原 Agent 类型的详情弹窗与执行步骤入口', async () => {
  const row = { id: 1, type: 'agent', system_kind: 'command', execution_id: 'a'.repeat(32),
    status: 'completed', duration: 20, content: '/查询 玩家', replay_context: '指令已完成',
    request_context: null, data: null };
  vi.stubGlobal('fetch', vi.fn(async url => Response.json(String(url).includes('/count?')
    ? { total: 1 } : String(url).includes('?') ? { items: [row], next_cursor: null } : row)));
  render(<Sessions token="offline" />);
  fireEvent.click(screen.getByRole('tab', { name: '系统调用' }));
  const button = await screen.findByRole('button', { name: '查看详情' });
  await waitFor(() => expect(button).toBeEnabled());
  fireEvent.click(button);
  const dialog = await screen.findByRole('dialog', { name: '调用详情 #1' });
  expect(await within(dialog).findByText('指令已完成')).toBeInTheDocument();
  expect(within(dialog).getByText(/类型：指令调用/)).toBeInTheDocument();
  expect(within(dialog).getByRole('button', { name: '查看执行步骤与附件' })).toBeInTheDocument();
});
