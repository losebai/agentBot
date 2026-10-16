import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { CallRecords } from './CallRecords';

const record = {
  id: 1, type: 'ordinary', session_Id: 'session', status: 'completed', model: 'model',
  duration: 20, create_time: '2026-10-15T12:00:00', source: 'python', source_id: '1',
  request_id: 'request', execution_id: null, content: '输入正文', replay_context: '回复正文',
  user_certificate: null, request_context: null, data: null,
  usage: { prompt_tokens: 1000, completion_tokens: 200, total_tokens: 1200,
    cached_tokens: 300, cache_prompt_tokens: 500 },
};

it.each(['conversation', 'system'] as const)('%s 列表和详情显示 Token 用量，缓存率按已上报的输入计算', async category => {
  vi.stubGlobal('fetch', vi.fn(async url => Response.json(String(url).includes('?')
    ? { items: [record], next_cursor: null } : record)));
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
  vi.stubGlobal('fetch', vi.fn(async () => Response.json({ items: [zero, unknown], next_cursor: null })));
  render(<CallRecords token="offline" />);
  const buttons = await screen.findAllByRole('button', { name: '查看详情' });
  const zeroRow = within(buttons[0].closest('tr')!);
  expect(zeroRow.getByText('缓存').parentElement).toHaveTextContent('0');
  expect(zeroRow.getByText('缓存命中率').parentElement).toHaveTextContent('0.0%');
  const unknownRow = within(buttons[1].closest('tr')!);
  expect(unknownRow.getAllByText('—')).toHaveLength(5);
  expect(unknownRow.queryByText('0')).not.toBeInTheDocument();
});
