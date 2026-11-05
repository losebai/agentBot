import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { Autonomy } from './Autonomy';

const profile = { id: 'profile-one', revision: 3, owner: { sender_id: '10002' },
  identity: { name: '阿澄', role: '长期助手', style: '简洁' },
  policy: { paused: true, allowed_actions: ['recall', 'report'] },
  state: { current_goal: '', current_task: null, activity: 'sleeping', emotion: 'neutral', recent_errors: [] }, usage: {} };
const settings = { database_revision: 7, fields: [
  { name: 'chat_active_actions_paused', value: false }, { name: 'chat_autonomy_capture_enabled', value: false },
] };

/** 持久身份页面始终使用离线响应，测试不启动后台执行器或连接真实机器人。 */
function mockApi() {
  const writes: { url: string; body: unknown }[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input, init) => {
    const url = String(input);
    if (init?.method && init.method !== 'GET') {
      writes.push({ url, body: init.body ? JSON.parse(String(init.body)) : undefined });
      if (url.endsWith('/autonomy/pause')) return new Response(null, { status: 204 });
      return Response.json({ ...profile, revision: 4 });
    }
    if (url.endsWith('/settings')) return Response.json(settings);
    if (url.includes('/autonomy/profiles?')) return Response.json([profile]);
    if (url.includes('/tasks?')) return Response.json([{ id: 'task-one', revision: 8, status: 'approval',
      goal: '整理资料后汇报', steps: [{ action: 'report' }], cursor: 0, outputs: [], error_code: 'REPORT_APPROVAL_REQUIRED' }]);
    if (url.includes('/conversations?')) return Response.json({ items: [], next_before: null });
    if (url.includes('/audit?')) return Response.json({ items: [], next_before: null });
    if (url.includes('/proposals?')) return Response.json([]);
    throw new Error('未设置的离线请求：' + url);
  }));
  return writes;
}

it('暂停使用专用无版本入口，宿主开关只提交当前修改字段', async () => {
  const writes = mockApi();
  render(<Autonomy token="offline" />);
  expect(await screen.findByRole('tab', { name: '当前状态' })).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: /暂停全部身份/ }));
  await waitFor(() => expect(writes[0]).toEqual({ url: '/api/v1/autonomy/pause', body: undefined }));
  await waitFor(() => expect(screen.getByRole('switch', { name: '主动行动总暂停' })).toBeEnabled());
  fireEvent.click(screen.getByRole('switch', { name: '主动行动总暂停' }));
  await waitFor(() => expect(writes[1]).toEqual({ url: '/api/v1/settings',
    body: { expected_revision: 7, values: { chat_active_actions_paused: true } } }));
});

it('计划审核提交所见任务版本，状态与身份设定分开呈现', async () => {
  const writes = mockApi();
  render(<Autonomy token="offline" />);
  expect(await screen.findByRole('tab', { name: '当前状态' })).toBeVisible();
  fireEvent.click(screen.getByRole('tab', { name: '当前状态' }));
  expect(screen.getByText('休眠')).toBeVisible();
  fireEvent.click(await screen.findByRole('tab', { name: '任务 (1)' }));
  fireEvent.click(await screen.findByRole('button', { name: '批准当前计划' }));
  await waitFor(() => expect(writes[0]).toEqual({
    url: '/api/v1/autonomy/profiles/profile-one/tasks/task-one/decision',
    body: { expected_revision: 8, approve: true },
  }));
});
