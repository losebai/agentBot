import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { AutonomyApi } from '../autonomy-api';
import type { ConversationEntry, Profile } from '../autonomy-api';
import { AutonomyConversation } from './AutonomyConversation';

const profile = { id: 'profile-one', identity: { name: '阿澄' }, owner: { sender_id: '10002' } } as Profile;
const task: ConversationEntry = { id: 'new', created_at: '2026-11-03T09:00:00Z', episode: null, source_expired: false,
  task: { id: 'task', revision: 1, goal: '核对部署资料', status: 'succeeded', cursor: 2, attempts: 0,
    error_code: null, steps: [{ action: 'draft', text: '整理结论', tags: [] }, { action: 'report', text: '', tags: [] }],
    outputs: ['版本已核对，资料完整。', { text: '核对完成，等待发送。', queued: true, delivery: 'queued' }] } };
const source: ConversationEntry = { id: 'old', created_at: '2026-11-03T08:00:00Z', task: null, source_expired: false,
  episode: { id: 'episode', text: '原始用户发言', source: 'self_report', occurred_at: '2026-11-03T08:00:00Z', summary: null } };

/** 离线请求保留真实客户端路径，任何非只读请求均令测试失败。 */
function mockApi() {
  const fetch = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
    expect(init?.method).toBe('GET');
    const older = String(_input).includes('before=new');
    return Response.json({ items: older ? [source] : [task], next_before: older ? null : 'new' });
  });
  vi.stubGlobal('fetch', fetch);
  return fetch;
}

it('按对话显示目标和真实步骤结果，入队不标记已投递，支持更早来源和刷新', async () => {
  const fetch = mockApi();
  render(<AutonomyConversation api={new AutonomyApi('offline')} profile={profile} active />);
  expect(await screen.findByText('版本已核对，资料完整。')).toBeVisible();
  expect(screen.getByText('待投递')).toBeVisible();
  expect(screen.queryByText('已投递')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '更早对话' }));
  fireEvent.click(await screen.findByRole('button', { name: /原始用户发言/ }));
  expect(screen.getByText('用户 10002')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '刷新对话' }));
  expect(await screen.findByText('版本已核对，资料完整。')).toBeVisible();
  expect(fetch).toHaveBeenCalledTimes(3);
});

it('离开页签取消读取，过期来源仅显示墓碑且可在失败后重试', async () => {
  const api = new AutonomyApi('offline');
  const request = vi.spyOn(api, 'conversations').mockRejectedValueOnce(new Error('读取失败'))
    .mockResolvedValue({ items: [{ ...source, episode: null, source_expired: true }], next_before: null });
  const view = render(<AutonomyConversation api={api} profile={profile} active />);
  expect(await screen.findByText('读取失败')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '刷新对话' }));
  expect(await screen.findByText('来源情节已过期')).toBeVisible();
  expect(screen.queryByText('原始用户发言')).toBeNull();
  view.rerender(<AutonomyConversation api={api} profile={profile} active={false} />);
  await waitFor(() => expect(request).toHaveBeenCalledTimes(2));
});


it('本地身份能直接发送，失败重试复用幂等键且不请求QQ接口', async () => {
  const api = new AutonomyApi('offline');
  vi.spyOn(api, 'conversations').mockResolvedValue({ items: [], next_before: null });
  const send = vi.spyOn(api, 'send').mockRejectedValueOnce(new Error('连接中断'))
    .mockResolvedValue(task.task!);
  const local = { ...profile, owner: { channel: 'local' as const, agent_key: 'alice', participant_id: 'human', session_id: 'local' }, policy: { paused: true } } as Profile;
  render(<AutonomyConversation api={api} profile={local} active />);
  fireEvent.change(screen.getByLabelText('发送给独立智能体'), { target: { value: '你好，独立Agent' } });
  fireEvent.click(screen.getByRole('button', { name: '发送消息' }));
  expect(await screen.findByText('连接中断')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '发送消息' }));
  await waitFor(() => expect(send).toHaveBeenCalledTimes(2));
  expect(send.mock.calls[0]).toEqual(send.mock.calls[1]);
  await waitFor(() => expect(screen.getByLabelText('发送给独立智能体')).toHaveValue(''));
  expect(screen.getByText('身份已暂停，消息保存后等待恢复')).toBeVisible();
});
