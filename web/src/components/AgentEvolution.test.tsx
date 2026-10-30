import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { AgentEvolution } from './AgentEvolution';

const profile = { id: 7, agent_id: 1, scope_key: 'agent:1', subject_key: 'a'.repeat(64), revision: 3,
  traits: { verbosity: -1, warmth: 0, humor: 0, initiative: 0 } };

/** 使用真实 HTTP 边界替身，控制并发失败但保留组件状态与交互。 */
function server({ failPause = false, failReset = false } = {}) {
  const writes: Array<{ url: string; body: Record<string, unknown> }> = [];
  let currentProfile = profile;
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? 'GET';
    if (method !== 'GET') {
      const body = JSON.parse(String(init?.body));
      writes.push({ url, body });
      if ((method === 'PATCH' && failPause) || (url.endsWith('/reset') && failReset)) {
        return new Response(JSON.stringify({ message: '版本已变化，请刷新', code: 'CONFLICT' }), { status: 409 });
      }
      if (method === 'PATCH') return new Response(JSON.stringify({ enabled: body.enabled, revision: 1 }));
      currentProfile = { ...profile, revision: 4, traits: { verbosity: 0, warmth: 0, humor: 0, initiative: 0 } };
      return new Response(JSON.stringify(currentProfile));
    }
    if (url.includes('/records?')) return Response.json({ items: [], next_cursor: null });
    if (url.includes('/profiles/7')) return new Response(JSON.stringify({ profile: currentProfile,
      items: [{ id: 12, kind: 'change', dimension: 'verbosity', direction: -1,
        excerpt: '长期明确反馈达到门槛', observed_at: '2026-01-08T00:00:00',
        before_value: 0, after_value: -1, evidence_ids: [1, 4, 9] },
      { id: 9, kind: 'feedback', dimension: 'verbosity', direction: -1,
        excerpt: '以后请简洁一点', observed_at: '2026-01-08T00:00:00',
        before_value: null, after_value: null, evidence_ids: [] }], next_cursor: null }));
    return new Response(JSON.stringify({ config: { enabled: true, revision: 0 }, items: [profile], next_cursor: null }));
  }));
  return writes;
}

it('进入演进页面直接加载状态，并能查看变化的真实证据', async () => {
  server();
  render(<AgentEvolution token="offline" agentId={1} />);
  await screen.findByText('互动对象 aaaaaaaa');
  fireEvent.click(screen.getByRole('button', { name: '查看演化记录' }));
  await screen.findByText('以后请简洁一点');
  expect(screen.getByText('依据反馈 #1、#4、#9')).toBeInTheDocument();
});

it('确认重置成功后更新档位，同时保留历史反馈供追溯', async () => {
  server();
  render(<AgentEvolution token="offline" agentId={1} />);
  fireEvent.click(await screen.findByRole('button', { name: '查看演化记录' }));
  await screen.findByText('以后请简洁一点');
  fireEvent.click(screen.getByRole('button', { name: '重置此对象的习惯' }));
  fireEvent.click(screen.getByRole('button', { name: '确认重置' }));
  await screen.findByText('习惯已重置，旧反馈不再参与演化。');
  await waitFor(() => expect(screen.queryByText(/详略 -1/)).not.toBeInTheDocument());
  expect(screen.getByText('以后请简洁一点')).toBeInTheDocument();
});

it('暂停失败保留当前开关，显示错误而不伪装保存成功', async () => {
  const writes = server({ failPause: true });
  render(<AgentEvolution token="offline" agentId={1} />);
  const toggle = await screen.findByRole('checkbox', { name: '启用长期互动演化' });
  fireEvent.click(toggle);
  await screen.findByText('版本已变化，请刷新');
  expect(toggle).toBeChecked();
  expect(writes[0].body).toEqual({ enabled: false, expected_revision: 0 });
  expect(writes[0].url).toBe('/api/v1/evolution?agent_id=1');
});

it('重置需确认并绑定当前画像版本，冲突保留证据供用户刷新', async () => {
  const writes = server({ failReset: true });
  render(<AgentEvolution token="offline" agentId={1} />);
  fireEvent.click(await screen.findByRole('button', { name: '查看演化记录' }));
  await screen.findByText('以后请简洁一点');
  fireEvent.click(screen.getByRole('button', { name: '重置此对象的习惯' }));
  expect(writes).toHaveLength(0);
  fireEvent.click(screen.getByRole('button', { name: '确认重置' }));
  await screen.findByText('版本已变化，请刷新');
  expect(writes[0].body).toEqual({ expected_revision: 3 });
  expect(writes[0].url).toBe('/api/v1/evolution/profiles/7/reset');
  expect(screen.getByText('以后请简洁一点')).toBeInTheDocument();
});

it('切换 Agent 后取消旧读取，旧响应不能覆盖新对象', async () => {
  let complete: (value: Response) => void = () => {};
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => String(input).includes('agent_id=1')
    ? new Promise<Response>(resolve => { complete = resolve; })
    : Promise.resolve(new Response(JSON.stringify({ config: { enabled: false, revision: 2 }, items: [], next_cursor: null })))));
  const view = render(<AgentEvolution token="offline" agentId={1} />);
  view.rerender(<AgentEvolution token="offline" agentId={2} />);
  await screen.findByText('尚未积累明确反馈。');
  complete(new Response(JSON.stringify({ config: { enabled: true, revision: 0 }, items: [profile], next_cursor: null })));
  await waitFor(() => expect(screen.queryByText('互动对象 aaaaaaaa')).not.toBeInTheDocument());
  expect(screen.getByRole('checkbox', { name: '启用长期互动演化' })).not.toBeChecked();
});

it('未关联角色时读取通用状态并独立保存启停配置', async () => {
  const requests: string[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input); requests.push(url);
    if (init?.method === 'PATCH') return Response.json({ enabled: false, revision: 1 });
    if (url.includes('/records?')) return Response.json({ items: [], next_cursor: null });
    return Response.json({ config: { enabled: true, revision: 0 }, items: [], next_cursor: null });
  }));
  render(<AgentEvolution token="offline" />);
  fireEvent.click(await screen.findByRole('checkbox', { name: '启用长期互动演化' }));
  await screen.findByText('已暂停学习和习惯应用。');
  expect(requests).toContain('/api/v1/evolution?cursor=0');
  expect(requests).toContain('/api/v1/evolution');
  expect(requests.some(url => url.includes('/agents/') || url.includes('agent_id='))).toBe(false);
});
