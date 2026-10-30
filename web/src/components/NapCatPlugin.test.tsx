import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { ApiClient } from '../api';
import { NapCatPlugin } from './NapCatPlugin';

/** 只模拟本机管理接口，测试不会打开浏览器或访问真实 QQ。 */
function setup() {
  const api = new ApiClient('offline');
  vi.spyOn(api, 'napcatPlugin').mockResolvedValue({ available: true, plugin_enabled: true,
    status: { mode: 'managed', state: 'running', webui_ready: true, error_code: null }, webui_port: 6199 });
  vi.spyOn(api, 'napcatLogin').mockResolvedValue({ status: 'waiting', qr_code_url: 'https://txz.qq.com/p?k=offline' });
  vi.spyOn(api, 'refreshNapcatLogin').mockResolvedValue({ status: 'scanned', qr_code_url: null });
  vi.spyOn(api, 'openNapcatAdmin').mockResolvedValue({ accepted: true });
  return api;
}

it('直接展示二维码和活动端口，通过无参数意图打开浏览器后台', async () => {
  const api = setup();
  render(<NapCatPlugin api={api} />);
  expect(await screen.findByRole('img', { name: 'QQ 登录二维码' })).toBeVisible();
  expect(screen.getByText('6199')).toBeVisible();
  expect(screen.queryByRole('button', { name: '打开登录页' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '打开后台管理界面' }));
  await waitFor(() => expect(api.openNapcatAdmin).toHaveBeenCalledWith());
  await waitFor(() => expect(screen.getByRole('button', { name: '刷新二维码' })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: '刷新二维码' }));
  expect(await screen.findByText('已扫码，请在手机上确认登录')).toBeVisible();
  expect(screen.queryByRole('img', { name: 'QQ 登录二维码' })).not.toBeInTheDocument();
});

it('离开配置页取消扫码轮询，并丢弃晚到的二维码', async () => {
  const api = setup();
  let signal: AbortSignal | undefined;
  let complete: ((value: { status: 'waiting'; qr_code_url: string }) => void) | undefined;
  vi.mocked(api.napcatLogin).mockImplementation(value => {
    signal = value;
    return new Promise(resolve => { complete = resolve; });
  });
  const view = render(<NapCatPlugin api={api} />);
  await waitFor(() => expect(complete).toBeDefined());
  view.unmount();
  expect(signal?.aborted).toBe(true);
  await act(async () => complete!({ status: 'waiting', qr_code_url: 'https://txz.qq.com/p?k=late' }));
  expect(screen.queryByRole('img', { name: 'QQ 登录二维码' })).not.toBeInTheDocument();
});

it('登录成功后收起二维码，停止服务后清除活动端口', async () => {
  const api = setup();
  vi.mocked(api.napcatLogin).mockResolvedValue({ status: 'online', qr_code_url: null });
  render(<NapCatPlugin api={api} />);
  expect(await screen.findByText('QQ 已登录')).toBeVisible();
  expect(screen.queryByRole('img', { name: 'QQ 登录二维码' })).not.toBeInTheDocument();
  vi.mocked(api.napcatPlugin).mockResolvedValue({ available: true, plugin_enabled: true,
    status: { mode: 'managed', state: 'stopped', webui_ready: false, error_code: null }, webui_port: null });
  await waitFor(() => expect(screen.getByText('未监听')).toBeVisible(), { timeout: 4000 });
  expect(screen.queryByText('QQ 已登录')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: '打开后台管理界面' })).toBeDisabled();
});
