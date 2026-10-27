import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { ApiClient } from '../api';
import { BilibiliPlugin } from './BilibiliPlugin';

afterEach(() => vi.unstubAllGlobals());

it('禁用插件不发起账号请求', () => {
  const fetcher = vi.fn(); vi.stubGlobal('fetch', fetcher);
  render(<BilibiliPlugin api={new ApiClient('offline')} enabled={false} />);
  expect(screen.getByText('请先启用 B站插件并重启后端。')).toBeVisible();
  expect(fetcher).not.toHaveBeenCalled();
});

it('过期的已保存登录仍能由管理员清除', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => Response.json({
    logged_in: false, status: 'expired', credential_set: true,
  })));
  render(<BilibiliPlugin api={new ApiClient('offline')} enabled />);
  await screen.findByText('登录已失效');
  expect(screen.getByRole('button', { name: '清除本地登录' })).toBeEnabled();
});

it('扫码确认后刷新账号，退出时取消旧轮询', async () => {
  let logged = false;
  const fetcher = vi.fn(async (url: string) => {
    if (String(url).endsWith('/status')) return Response.json(logged
      ? { logged_in: true, status: 'logged_in', uid: '12345', name: '离线账号' }
      : { logged_in: false, status: 'not_logged_in' });
    if (String(url).endsWith('/qr')) return Response.json({ session_id: 'opaque',
      url: 'https://passport.bilibili.com/qrcode/offline', expires_at: 9999999999, status: 'waiting' });
    logged = true;
    return Response.json({ status: 'confirmed' });
  });
  vi.stubGlobal('fetch', fetcher);
  const view = render(<BilibiliPlugin api={new ApiClient('offline')} enabled />);
  await screen.findByText('未登录');
  fireEvent.click(screen.getByRole('button', { name: '扫码登录' }));
  expect(await screen.findByText('离线账号 · UID 12345')).toBeVisible();
  await waitFor(() => expect(screen.queryByText('等待扫码')).not.toBeInTheDocument());
  view.unmount();
});
