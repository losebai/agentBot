import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it, vi } from 'vitest';
import { Overview } from './Overview';

/** 用离线接口观察真实指标与刷新，不连接服务或模型。 */
function status(statistics: object | null) {
  return { service: 'agentBot', ready: true, bot_count: 0, database_revision: 4,
    applied_revision: 4, restart_required_fields: [], statistics };
}

it('首页显示所选日期用量和加权缓存命中率，刷新更新正在运行数量', async () => {
  let running = 3;
  vi.stubGlobal('fetch', vi.fn(async url => Response.json(String(url).endsWith('/bots') ? [] : status({
    conversation_count: 1200, running_requests: running, total_tokens: 12345,
    prompt_tokens: 12000, completion_tokens: 345, cached_tokens: 3000,
    cache_prompt_tokens: 12000, cache_hit_rate: 0.25,
  }))));
  render(<Overview token="offline" />);
  await screen.findByText('对话总数');
  expect(screen.getByText('1,200')).toBeInTheDocument();
  expect(screen.getByText('当前运行中').parentElement).toHaveTextContent('3');
  expect(screen.getByText('Token 消耗').parentElement).toHaveTextContent('12,345');
  expect(screen.getByText('缓存命中率').parentElement).toHaveTextContent('25.0%');
  expect(screen.queryByText(/视频、通用 Laya 和 SolonCode 尚未迁移/)).not.toBeInTheDocument();
  running = 0;
  // 使用完整点击序列，等待初次加载后的按钮状态更新再刷新。
  const refresh = screen.getByText('刷新状态').closest('button')!;
  expect(refresh).toBeVisible();
  await userEvent.click(refresh);
  await waitFor(() => expect(screen.getByText('当前运行中').parentElement).toHaveTextContent('0'));
});

it('缓存信息未上报时显示未知，不能当作零命中', async () => {
  vi.stubGlobal('fetch', vi.fn(async url => Response.json(String(url).endsWith('/bots') ? [] : status({
    conversation_count: 1, running_requests: 0, total_tokens: 100,
    prompt_tokens: 100, completion_tokens: 0, cached_tokens: 0,
    cache_prompt_tokens: 0, cache_hit_rate: null,
  }))));
  render(<Overview token="offline" />);
  await screen.findByText('缓存命中率');
  expect(screen.getByText('缓存命中率').parentElement).toHaveTextContent('暂无缓存用量');
  expect(screen.queryByText('0.0%')).not.toBeInTheDocument();
});

it('归档未初始化时明确提示统计不可用，并保留服务状态', async () => {
  vi.stubGlobal('fetch', vi.fn(async url => Response.json(String(url).endsWith('/bots') ? [] : {
    ...status(null), statistics_error: 'AI_NOT_INITIALIZED',
  })));
  render(<Overview token="offline" />);
  expect(await screen.findByText(/调用记录尚未初始化/)).toBeInTheDocument();
  expect(screen.getByText('服务状态').parentElement).toHaveTextContent('就绪');
  expect(screen.getByText('对话总数').parentElement).toHaveTextContent('—');
});

it('托管状态读取失败仍显示已有用量和机器人连接', async () => {
  vi.stubGlobal('fetch', vi.fn(async url => {
    if (String(url).endsWith('/napcat/status')) throw new Error('offline observation failure');
    return Response.json(String(url).endsWith('/bots') ? [{ id: '12345', adapter: 'OneBot V11' }] : status({
      conversation_count: 9, running_requests: 0, total_tokens: 100, prompt_tokens: 90,
      completion_tokens: 10, cached_tokens: 0, cache_prompt_tokens: 0, cache_hit_rate: null,
    }));
  }));
  render(<Overview token="offline" />);
  expect(await screen.findByText(/NapCat 状态读取失败/)).toBeInTheDocument();
  expect(screen.getByText('12345')).toBeInTheDocument();
  expect(screen.getByText('对话总数').parentElement).toHaveTextContent('9');
});

it('默认按北京时间今日查询，区间筛选、刷新与今日重置共用统计条件', async () => {
  const today = new Date(Date.now() + 8 * 3600 * 1000).toISOString().slice(0, 10);
  vi.stubGlobal('fetch', vi.fn(async raw => {
    const url = new URL(String(raw), 'http://localhost');
    if (url.pathname.endsWith('/bots')) return Response.json([]);
    const current = url.searchParams.get('start_date') === today
      && url.searchParams.get('end_date') === today;
    const range = url.searchParams.get('start_date') === '2025-01-01'
      && url.searchParams.get('end_date') === '2025-01-03';
    return Response.json(status({
      conversation_count: current ? 7 : range ? 12 : 99, running_requests: 2,
      total_tokens: 100, cache_hit_rate: null,
      model_call_count: current ? 4 : 10, failed_model_calls: current ? 1 : 5,
      model_failure_rate: current ? 0.25 : 0.5,
    }));
  }));
  render(<Overview token="offline" />);
  await screen.findByText('模型调用失败率');
  expect(screen.getByLabelText('开始日期')).toHaveValue(today);
  expect(screen.getByLabelText('结束日期')).toHaveValue(today);
  expect(screen.getByText('对话总数').parentElement).toHaveTextContent('7');
  expect(screen.getByText('模型调用失败率').parentElement).toHaveTextContent('25.0%');
  fireEvent.change(screen.getByLabelText('开始日期'), { target: { value: '2025-01-01' } });
  fireEvent.change(screen.getByLabelText('结束日期'), { target: { value: '2025-01-03' } });
  await userEvent.click(screen.getByRole('button', { name: /查\s*询/ }));
  await waitFor(() => expect(screen.getByText('对话总数').parentElement).toHaveTextContent('12'));
  expect(screen.getByText('模型调用失败率').parentElement).toHaveTextContent('50.0%');
  await userEvent.click(screen.getByRole('button', { name: /刷新状态/ }));
  await waitFor(() => expect(screen.getByText('对话总数').parentElement).toHaveTextContent('12'));
  await userEvent.click(screen.getByRole('button', { name: /今\s*日/ }));
  await waitFor(() => expect(screen.getByText('对话总数').parentElement).toHaveTextContent('7'));
});

it('没有已结束的模型调用时失败率显示未知，日期倒置时阻止查询', async () => {
  vi.stubGlobal('fetch', vi.fn(async raw => Response.json(String(raw).endsWith('/bots') ? [] : status({
    conversation_count: 0, running_requests: 0, total_tokens: 0,
    cache_hit_rate: null, model_call_count: 0, failed_model_calls: 0,
    model_failure_rate: null,
  }))));
  render(<Overview token="offline" />);
  await screen.findByText('模型调用失败率');
  expect(screen.getByText('模型调用失败率').parentElement).toHaveTextContent('暂无已结束的模型调用');
  fireEvent.change(screen.getByLabelText('开始日期'), { target: { value: '2026-10-29' } });
  fireEvent.change(screen.getByLabelText('结束日期'), { target: { value: '2026-10-28' } });
  expect(screen.getByRole('button', { name: /查\s*询/ })).toBeDisabled();
});
