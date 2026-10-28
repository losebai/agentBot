import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { CallRecords } from './CallRecords';
import { chooseSelect } from '../test-select';

const record = {
  id: 1, type: 'ordinary', session_Id: 'session', status: 'completed', model: 'model',
  duration: 20, create_time: '2026-10-15T12:00:00', source: 'python', source_id: '1',
  request_id: 'request', execution_id: null, content: '输入正文', replay_context: '回复正文',
  user_certificate: null, request_context: null, data: null,
  usage: { prompt_tokens: 1000, completion_tokens: 200, total_tokens: 1200,
    cached_tokens: 300, cache_prompt_tokens: 500 },
};

it.each(['conversation', 'system'] as const)('%s 列表和详情显示 Token 用量，缓存率按已上报的输入计算', async category => {
  vi.stubGlobal('fetch', vi.fn(async url => Response.json(String(url).includes('/count?')
    ? { total: 1 } : String(url).includes('?') ? { items: [record], next_cursor: null } : record)));
  render(<CallRecords token="offline" category={category} />);
  const button = await screen.findByRole('button', { name: '查看详情' });
  const row = within(button.closest('tr')!);
  expect(row.getByText('输入').parentElement).toHaveTextContent('1,000');
  expect(row.getByText('输出').parentElement).toHaveTextContent('200');
  expect(row.getByText('缓存').parentElement).toHaveTextContent('300');
  expect(row.getByText('总计').parentElement).toHaveTextContent('1,200');
  expect(row.getByText('缓存命中率').parentElement).toHaveTextContent('60.0%');
  fireEvent.click(button);
  const dialog = within(await screen.findByRole('dialog', { name: '调用详情 #1' }));
  await waitFor(() => expect(dialog.getByText('Token 用量')).toBeVisible());
  expect(dialog.getByText('输入').parentElement).toHaveTextContent('1,000');
  expect(dialog.getByText('缓存命中率').parentElement).toHaveTextContent('60.0%');
  expect(dialog.getByText(/缓存统计覆盖的输入/)).toHaveTextContent('500');
});

it('明确零命中显示 0，未上报用量显示未知', async () => {
  const zero = { ...record, usage: { prompt_tokens: 100, completion_tokens: 0,
    total_tokens: 100, cached_tokens: 0, cache_prompt_tokens: 100 } };
  const unknown = { ...record, id: 2, usage: { prompt_tokens: null, completion_tokens: null,
    total_tokens: null, cached_tokens: null, cache_prompt_tokens: null } };
  vi.stubGlobal('fetch', vi.fn(async url => Response.json(String(url).includes('/count?')
    ? { total: 2 } : { items: [zero, unknown], next_cursor: null })));
  render(<CallRecords token="offline" />);
  const buttons = await screen.findAllByRole('button', { name: '查看详情' });
  const zeroRow = within(buttons[0].closest('tr')!);
  expect(zeroRow.getByText('缓存').parentElement).toHaveTextContent('0');
  expect(zeroRow.getByText('缓存命中率').parentElement).toHaveTextContent('0.0%');
  const unknownRow = within(buttons[1].closest('tr')!);
  expect(unknownRow.getAllByText('—')).toHaveLength(5);
  expect(unknownRow.queryByText('0')).not.toBeInTheDocument();
});

it('数字页码可直接跳转，返回已读页复用缓存，刷新及更改页大小重新加载', async () => {
  const requests: URL[] = [];
  vi.stubGlobal('fetch', vi.fn(async url => {
    const request = new URL(String(url), 'http://offline');
    requests.push(request);
    if (request.pathname.endsWith('/count')) return Response.json({ total: 101 });
    const number = Number(request.searchParams.get('page') ?? 1);
    return Response.json({ items: [{ ...record, id: number }], next_cursor: 'more' });
  }));
  render(<CallRecords token="offline" />);
  expect(await screen.findByText(/共 101 条/)).toBeVisible();
  const first = requests.find(request => !request.pathname.endsWith('/count'))!;
  expect(first.searchParams.get('limit')).toBe('20');
  fireEvent.change(screen.getByRole('spinbutton', { name: '跳转页码' }), { target: { value: '6' } });
  fireEvent.click(screen.getByRole('button', { name: '跳转' }));
  await waitFor(() => expect(screen.getByText('#6')).toBeVisible());
  fireEvent.click(screen.getByRole('button', { name: '第 1 页' }));
  await waitFor(() => expect(screen.getByText('#1')).toBeVisible());
  fireEvent.click(screen.getByRole('button', { name: '第 3 页' }));
  await waitFor(() => expect(screen.getByText('#3')).toBeVisible());
  expect(requests.at(-1)?.searchParams.get('page')).toBe('3');
  expect(screen.getByRole('button', { name: '第 3 页' })).toHaveAttribute('aria-current', 'page');
  const readRequests = requests.length;
  fireEvent.click(screen.getByRole('button', { name: '第 1 页' }));
  await waitFor(() => expect(screen.getByText('#1')).toBeVisible());
  expect(requests).toHaveLength(readRequests);
  expect(requests.filter(request => request.pathname.endsWith('/count'))).toHaveLength(1);
  await chooseSelect(screen.getByRole('combobox', { name: '每页条数' }), '50 条/页');
  await waitFor(() => expect(requests.at(-1)?.searchParams.get('limit')).toBe('50'));
  fireEvent.click(screen.getByRole('button', { name: '第 3 页' }));
  await waitFor(() => expect(screen.getByText('#3')).toBeVisible());
  fireEvent.click(screen.getByRole('button', { name: /刷新记录/ }));
  await waitFor(() => expect(screen.getByText('#1')).toBeVisible());
  expect(requests.filter(request => request.pathname.endsWith('/count'))).toHaveLength(2);
});

it('总数统计未完成时即可查看首屏，长上下文只在展开后格式化', async () => {
  let resolveCount!: (response: Response) => void;
  vi.stubGlobal('fetch', vi.fn(async url => {
    const path = String(url);
    if (path.includes('/count?')) return new Promise<Response>(resolve => { resolveCount = resolve; });
    if (path.includes('?')) return Response.json({ items: [record], next_cursor: null });
    return Response.json({ ...record, request_context: { large: '长上下文只按需展示' },
      data: { large: '响应扩展只按需展示' } });
  }));
  render(<CallRecords token="offline" />);
  fireEvent.click(await screen.findByRole('button', { name: '查看详情' }));
  const dialog = within(await screen.findByRole('dialog', { name: '调用详情 #1' }));
  await waitFor(() => expect(dialog.getByText('回复正文')).toBeVisible());
  expect(dialog.queryByText(/长上下文只按需展示/)).not.toBeInTheDocument();
  expect(dialog.queryByText(/响应扩展只按需展示/)).not.toBeInTheDocument();
  fireEvent.click(dialog.getByText('请求上下文'));
  expect(dialog.getByText(/长上下文只按需展示/)).toBeVisible();
  resolveCount(Response.json({ total: 1 }));
  expect(await screen.findByText(/共 1 条/)).toBeVisible();
});

it('筛选重置页码和缓存，旧筛选迟到的计数不能覆盖当前总页数', async () => {
  let resolveOldCount!: (response: Response) => void;
  let oldCountSignal: AbortSignal | undefined;
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    const request = new URL(String(url), 'http://offline');
    const filtered = request.searchParams.get('session_id') === 'new-session';
    if (request.pathname.endsWith('/count')) {
      if (filtered) return Response.json({ total: 1 });
      oldCountSignal = init?.signal;
      return new Promise<Response>(resolve => { resolveOldCount = resolve; });
    }
    return Response.json({ items: [{ ...record, id: filtered ? 300 : Number(request.searchParams.get('page')) }],
      next_cursor: filtered ? null : 'next' });
  }));
  render(<CallRecords token="offline" />);
  await waitFor(() => expect(screen.getByRole('button', { name: '下一页' })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: '下一页' }));
  await waitFor(() => expect(screen.getByText('#2')).toBeVisible());
  fireEvent.change(screen.getByLabelText('会话 ID'), { target: { value: 'new-session' } });
  fireEvent.click(screen.getByRole('button', { name: /筛\s*选/ }));
  await waitFor(() => expect(screen.getByText('#300')).toBeVisible());
  expect(await screen.findByText(/共 1 条 · 第 1 \/ 1 页/)).toBeVisible();
  expect(oldCountSignal?.aborted).toBe(true);
  await act(async () => resolveOldCount(Response.json({ total: 999 })));
  expect(screen.getByText(/共 1 条 · 第 1 \/ 1 页/)).toBeVisible();
  expect(screen.getByRole('button', { name: '下一页' })).toBeDisabled();
});

it('配置刷新拒绝请求后结束加载，显示明确提示并允许手动刷新', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => Response.json({code: 'RUNTIME_REFRESHING',
    message: '配置正在更新，请稍后刷新页面'}, {status: 503})));
  render(<CallRecords token="offline" />);
  expect(await screen.findAllByText(/配置正在更新/)).toHaveLength(2);
  await waitFor(() => expect(screen.getByRole('button', {name: '刷新记录'})).toBeEnabled());
  expect(screen.queryByRole('status', {name: '正在加载记录'})).not.toBeInTheDocument();
  expect(screen.getByRole('status', {name: '分页信息'})).toHaveTextContent('总条数暂不可用');
});
