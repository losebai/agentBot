import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { useState } from 'react';
import { expect, it, vi } from 'vitest';
import { AgentApi } from '../agent-api';
import { AgentRunDetail } from '../components/AgentRunDetail';
import { CallRecords } from './CallRecords';
import { MediaRuns } from './MediaRuns';

const record = {
  id: 1, execution_id: 'a'.repeat(32), type: 'agent', session_Id: 'session', session_id: 'session',
  status: 'completed', model: 'model', duration: 20, duration_ms: 20,
  create_time: '2026-10-13T00:00:00', created_at: '2026-10-13T00:00:00', output_type: 'IMAGE',
  source: 'python', source_id: '1', request_context: null, data: null,
  content: '保存的正文', replay_context: '保存的回复', prompt: '用户输入', attachments: [],
};

it.each([
  ['调用详情 #1', CallRecords], ['媒体执行详情', MediaRuns],
] as const)('%s 加载时可关闭，取消请求且迟到结果不会重开弹窗', async (title, Page) => {
  let resolve!: (response: Response) => void;
  let signal: AbortSignal | undefined;
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    if (String(url).includes('?')) return Response.json({ items: [record], next_cursor: null });
    signal = init.signal;
    return new Promise<Response>(done => { resolve = done; });
  }));
  render(<Page token="offline" />);
  fireEvent.click(await screen.findByRole('button', { name: '查看详情' }));
  const dialog = await screen.findByRole('dialog', { name: title });
  await waitFor(() => expect(within(dialog).getByRole('status', { name: '正在加载详情' })).toBeVisible());
  fireEvent.click(within(dialog).getByRole('button', { name: '关闭详情' }));
  expect(signal?.aborted).toBe(true);
  await act(async () => resolve(Response.json(record)));
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(screen.queryByText('保存的正文')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: '查看详情' })).toBeVisible();
});

it.each([
  ['调用详情 #1', CallRecords], ['媒体执行详情', MediaRuns],
] as const)('%s 的读取错误显示在弹窗内，关闭后可重新查看', async (title, Page) => {
  let failed = true;
  vi.stubGlobal('fetch', vi.fn(async url => String(url).includes('?')
    ? Response.json({ items: [record], next_cursor: null })
    : failed ? Response.json({ message: '记录读取失败' }, { status: 503 }) : Response.json(record)));
  render(<Page token="offline" />);
  fireEvent.click(await screen.findByRole('button', { name: '查看详情' }));
  const dialog = await screen.findByRole('dialog', { name: title });
  await waitFor(() => expect(within(dialog).getByText('记录读取失败')).toBeVisible());
  fireEvent.click(within(dialog).getByRole('button', { name: '关闭详情' }));
  failed = false;
  fireEvent.click(screen.getByRole('button', { name: '查看详情' }));
  const reopened = await screen.findByRole('dialog', { name: title });
  await waitFor(() => expect(within(reopened).getByText('保存的正文')).toBeVisible());
  expect(screen.queryByText('记录读取失败')).not.toBeInTheDocument();
});

it('调用详情中的执行步骤打开独立弹窗，关闭后保留调用正文', async () => {
  vi.stubGlobal('fetch', vi.fn(async url => {
    if (String(url).includes('/steps')) return Response.json({ items: [], next_cursor: null });
    return Response.json(String(url).includes('?') ? { items: [record], next_cursor: null } : record);
  }));
  render(<CallRecords token="offline" />);
  fireEvent.click(await screen.findByRole('button', { name: '查看详情' }));
  const trigger = await screen.findByRole('button', { name: '查看执行步骤与附件' });
  trigger.focus();
  fireEvent.click(trigger);
  // Ant Design 测试环境使用固定标题 ID；通过标题定位所属弹窗，避免多弹窗同名误判。
  const steps = (await screen.findByText('Agent 执行详情')).closest('[role="dialog"]') as HTMLElement;
  await waitFor(() => expect(within(steps).getByText('工具步骤')).toBeVisible());
  fireEvent.click(within(steps).getByRole('button', { name: '关闭详情' }));
  expect(trigger).toHaveFocus();
  expect(screen.queryByText('Agent 执行详情')).not.toBeInTheDocument();
  expect(within(screen.getByRole('dialog', { name: '调用详情 #1' })).getByText('保存的回复')).toBeVisible();
});

/** 用真实卸载模拟父页面关闭详情，校验正文和步骤共用的取消边界。 */
function PendingAgent() {
  const [open, setOpen] = useState(true);
  return open ? <AgentRunDetail api={new AgentApi('offline')} id={record.execution_id} onClose={() => setOpen(false)} /> : null;
}

it('Agent 详情加载期间点关闭图标同时取消正文和步骤请求', async () => {
  const signals: AbortSignal[] = [];
  vi.stubGlobal('fetch', vi.fn((_url, init) => { signals.push(init.signal); return new Promise(() => {}); }));
  render(<PendingAgent />);
  await waitFor(() => expect(signals).toHaveLength(2));
  fireEvent.click(within(screen.getByRole('dialog', { name: 'Agent 执行详情' })).getByRole('button', { name: 'Close' }));
  expect(signals.every(signal => signal.aborted)).toBe(true);
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});
