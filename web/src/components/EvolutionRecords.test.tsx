import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { AgentEvolution } from './AgentEvolution';

const profile = { id: 7, agent_id: 2, scope_key: 'agent:2', subject_key: 'b'.repeat(64), revision: 3,
  traits: { verbosity: -1, warmth: 0, humor: 0, initiative: 0 } };
const record = { id: 12, profile_id: 7, kind: 'change', dimension: 'verbosity', direction: -1,
  excerpt: '长期明确反馈达到门槛', observed_at: '2026-01-08T00:00:00',
  before_value: 0, after_value: -1, evidence_ids: [1, 4, 9],
  subject_key: 'b'.repeat(64), agent_id: 2, agent_name: '写作助手', scope_key: 'agent:2' };

/** 只替换网络边界，保留列表筛选、分页与跨范围详情的真实状态。 */
function server() {
  const requests: string[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input); requests.push(url);
    if (url.includes('/profiles/7')) {
      if (init?.method === 'POST') return Response.json({ ...profile, revision: 4 });
      return Response.json({ profile, items: [record], next_cursor: null });
    }
    if (url.includes('/records?')) {
      const query = new URL(url, 'http://localhost').searchParams;
      if (query.get('unbound') === 'true') return Response.json({ items: [{ ...record, id: 10,
        agent_id: null, agent_name: null, scope_key: 'global', excerpt: '通用范围反馈', kind: 'feedback',
        before_value: null, after_value: null, evidence_ids: [] }], next_cursor: null });
      if (query.get('kind') === 'feedback') return Response.json({ items: [], next_cursor: null });
      if (query.get('cursor') === '12') return Response.json({ items: [{ ...record, id: 8,
        kind: 'reset', excerpt: '用户手动重置', dimension: '', before_value: null, after_value: null }], next_cursor: null });
      return Response.json({ items: [record], next_cursor: 12 });
    }
    return Response.json({ config: { enabled: true, revision: 0 }, items: [], next_cursor: null });
  }));
  return requests;
}

it('没有当前范围画像也默认显示所有范围的独立记录，并展示变化与匿名主体', async () => {
  const requests = server();
  render(<AgentEvolution token="offline" />);
  const list = await screen.findByRole('region', { name: '演化记录列表' });
  expect(await within(list).findByText('长期明确反馈达到门槛')).toBeVisible();
  expect(within(list).getByText('写作助手')).toBeVisible();
  expect(within(list).getByText('互动对象 bbbbbbbb')).toBeVisible();
  expect(within(list).getByText('详略')).toBeVisible();
  expect(within(list).getByText('0 → -1')).toBeVisible();
  expect(within(list).getByText('2026-01-08T00:00:00 UTC')).toBeVisible();
  expect(requests).toContain('/api/v1/evolution/records?cursor=0');
  expect(screen.getByText('尚未积累明确反馈。')).toBeVisible();
});

it('记录倒序游标追加更早页，类型和未关联范围筛选从首页重新读取', async () => {
  const requests = server();
  render(<AgentEvolution token="offline" />);
  fireEvent.click(await screen.findByRole('button', { name: '加载更早演化记录' }));
  await screen.findByText('用户手动重置');
  expect(requests).toContain('/api/v1/evolution/records?cursor=12');
  fireEvent.change(screen.getByLabelText('记录类型'), { target: { value: 'feedback' } });
  await screen.findByText('暂无符合条件的演化记录。');
  expect(screen.queryByText('用户手动重置')).not.toBeInTheDocument();
  expect(requests).toContain('/api/v1/evolution/records?cursor=0&kind=feedback');
  fireEvent.change(screen.getByLabelText('记录范围'), { target: { value: 'unbound' } });
  await screen.findByText('通用范围反馈');
  expect(requests).toContain('/api/v1/evolution/records?cursor=0&kind=feedback&unbound=true');
  expect(within(screen.getByRole('region', { name: '演化记录列表' })).getByText('未关联角色（通用）')).toBeVisible();
});

it('通用页面可按独立记录查看其他角色画像依据并按所见版本重置', async () => {
  const requests = server();
  render(<AgentEvolution token="offline" />);
  fireEvent.click(await screen.findByRole('button', { name: '查看依据' }));
  const detail = await screen.findByRole('region', { name: '演化记录' });
  expect(detail).toHaveFocus();
  expect(within(detail).getByText('依据反馈 #1、#4、#9')).toBeVisible();
  expect(requests).toContain('/api/v1/evolution/profiles/7?cursor=0');
  fireEvent.click(within(detail).getByRole('button', { name: '重置此对象的习惯' }));
  fireEvent.click(within(detail).getByRole('button', { name: '确认重置' }));
  await screen.findByText('习惯已重置，旧反馈不再参与演化。');
  expect(requests).toContain('/api/v1/evolution/profiles/7/reset');
});

it('筛选时取消旧记录请求，晚到响应不能混入新类型', async () => {
  let resolveOld: (value: Response) => void = () => {};
  let oldSignal: AbortSignal | null | undefined;
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.includes('/records?') && !url.includes('kind=')) {
      oldSignal = init?.signal;
      return new Promise<Response>(resolve => { resolveOld = resolve; });
    }
    return Promise.resolve(Response.json(url.includes('/records?') ? { items: [], next_cursor: null }
      : { config: { enabled: true, revision: 0 }, items: [], next_cursor: null }));
  }));
  render(<AgentEvolution token="offline" />);
  fireEvent.change(await screen.findByLabelText('记录类型'), { target: { value: 'reset' } });
  await screen.findByText('暂无符合条件的演化记录。');
  expect(oldSignal?.aborted).toBe(true);
  resolveOld(Response.json({ items: [record], next_cursor: 12 }));
  await waitFor(() => expect(screen.queryByText('长期明确反馈达到门槛')).not.toBeInTheDocument());
});
