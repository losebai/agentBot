import { fireEvent, render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { App } from './App';

it('管理凭据校验失败时保持登录页面且不持久化凭据', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => Response.json({ code: 'UNAUTHORIZED',
    message: '管理凭据无效', request_id: 'offline' }, { status: 401 })));
  const persist = vi.spyOn(Storage.prototype, 'setItem');
  render(<App />);
  fireEvent.change(screen.getByLabelText('管理员凭据'), { target: { value: 'wrong-token' } });
  fireEvent.click(screen.getByRole('button', { name: '连接服务' }));
  expect(await screen.findByText('管理凭据无效')).toBeInTheDocument();
  expect(screen.queryByRole('navigation')).not.toBeInTheDocument();
  expect(persist).not.toHaveBeenCalled();
  persist.mockRestore();
});

it('连接后显示真实状态与机器人数量，退出清空会话', async () => {
  vi.stubGlobal('fetch', vi.fn(async (url) => String(url).endsWith('/bots')
    ? Response.json([{ id: '123456', adapter: 'OneBot V11' }])
    : Response.json({ service: 'agentBot', ready: true, bot_count: 1,
      database_revision: 4, applied_revision: 3, restart_required_fields: ['port'],
      ai_backend: 'python-ordinary' })));
  render(<App />);
  fireEvent.change(screen.getByLabelText('管理员凭据'), { target: { value: 'offline-token' } });
  fireEvent.click(screen.getByRole('button', { name: '连接服务' }));
  expect(await screen.findByRole('navigation')).toBeInTheDocument();
  expect(await screen.findByText('123456')).toBeInTheDocument();
  expect(screen.getByText(/以下设置需要重启：port/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '退出管理' }));
  expect(screen.getByLabelText('管理员凭据')).toHaveValue('');
  expect(screen.queryByRole('navigation')).not.toBeInTheDocument();
});
