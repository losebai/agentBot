import { fireEvent, render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { App } from './App';

/** 使用离线响应验证登录流程，避免连接真实后端或机器人。 */
function managementResponse(url: string) {
  return url.endsWith('/bots')
    ? Response.json([{ id: '123456', adapter: 'OneBot V11' }])
    : Response.json({ service: 'agentBot', ready: true, bot_count: 1,
      database_revision: 4, applied_revision: 3, restart_required_fields: ['port'],
      ai_backend: 'python-ordinary' });
}

it('密码校验失败时保持登录页面且不持久化密码', async () => {
  vi.stubGlobal('fetch', vi.fn(async (url) => String(url).endsWith('/auth')
    ? Response.json({ password_required: true })
    : Response.json({ code: 'UNAUTHORIZED', message: '管理密码无效', request_id: 'offline' }, { status: 401 })));
  const persist = vi.spyOn(Storage.prototype, 'setItem');
  render(<App />);
  fireEvent.change(await screen.findByLabelText('管理员密码'), { target: { value: 'Wrong!123' } });
  fireEvent.click(screen.getByRole('button', { name: /登\s*录/ }));
  expect(await screen.findByText('管理密码无效')).toBeInTheDocument();
  expect(screen.queryByRole('navigation')).not.toBeInTheDocument();
  expect(persist).not.toHaveBeenCalled();
  persist.mockRestore();
});

it('自定义密码登录后显示真实状态，退出清空密码', async () => {
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    if (String(url).endsWith('/auth')) return Response.json({ password_required: true });
    if (new Headers(init?.headers).get('Authorization') !== 'Bearer Easy!123') {
      return Response.json({ message: '管理密码无效' }, { status: 401 });
    }
    return managementResponse(String(url));
  }));
  render(<App />);
  fireEvent.change(await screen.findByLabelText('管理员密码'), { target: { value: 'Easy!123' } });
  fireEvent.click(screen.getByRole('button', { name: /登\s*录/ }));
  expect(await screen.findByRole('navigation')).toBeInTheDocument();
  expect(await screen.findByText('123456')).toBeInTheDocument();
  expect(screen.getByText(/以下设置需要重启：port/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '退出管理' }));
  expect(screen.getByLabelText('管理员密码')).toHaveValue('');
  expect(screen.queryByRole('navigation')).not.toBeInTheDocument();
});

it('未配置密码时直接进入系统，管理请求不发送空认证头', async () => {
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    if (new Headers(init?.headers).has('Authorization')) {
      return Response.json({ message: '不应发送空认证头' }, { status: 400 });
    }
    return String(url).endsWith('/auth')
      ? Response.json({ password_required: false })
      : managementResponse(String(url));
  }));
  render(<App />);
  expect(await screen.findByRole('navigation')).toBeInTheDocument();
  expect(await screen.findByText('123456')).toBeInTheDocument();
  expect(screen.queryByLabelText('管理员密码')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '退出管理' })).not.toBeInTheDocument();
});

it('查询登录模式失败时显示错误，不能误进入系统', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => Response.json({ message: '服务暂时无法完成请求' }, { status: 503 })));
  render(<App />);
  expect(await screen.findByText('服务暂时无法完成请求')).toBeInTheDocument();
  expect(screen.queryByRole('navigation')).not.toBeInTheDocument();
  expect(screen.queryByLabelText('管理员密码')).not.toBeInTheDocument();
});
