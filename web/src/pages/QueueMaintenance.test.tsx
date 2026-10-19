import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { StrictMode } from 'react';
import { Settings } from './Settings';
import { QueueMaintenance } from './QueueMaintenance';

const settings = {
  database_revision: 1, applied_revision: 1, startup_revision: null, restart_required_fields: [],
  fields: [{ name: 'chat_max_sessions', description: '最大会话数', kind: 'number', value: 20,
    nullable: false, secret_is_set: false, restart_required: false }],
};

describe('队列维护', () => {
  it('严格模式重新挂载读取后仍展示计数，取消的旧请求不能覆盖状态', async () => {
    let completeOld!: (response: Response) => void;
    let reads = 0;
    vi.stubGlobal('fetch', vi.fn(async () => {
      reads += 1;
      if (reads === 1) return new Promise<Response>(resolve => { completeOld = resolve; });
      return Response.json({ available: true, text: 3, images: 1, total: 4 });
    }));
    render(<StrictMode><QueueMaintenance token="offline-token" /></StrictMode>);
    expect(await screen.findByText('文本死信：3 条')).toBeInTheDocument();
    await act(async () => {
      completeOld(Response.json({ available: true, text: 99, images: 99, total: 198 }));
    });
    expect(screen.getByText('文本死信：3 条')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '一键清除死信' })).toBeEnabled();
  });

  it('一键清除两类死信，阻止重复操作并保留未保存的配置', async () => {
    let complete!: (response: Response) => void;
    const requests: { url: string; method: string; authorization: string | null }[] = [];
    vi.stubGlobal('fetch', vi.fn(async (url, init) => {
      const method = init?.method || 'GET';
      requests.push({ url: String(url), method,
        authorization: new Headers(init?.headers).get('Authorization') });
      if (url === '/api/v1/settings') return Response.json(settings);
      if (method === 'DELETE') return new Promise<Response>(resolve => { complete = resolve; });
      return Response.json({ available: true, text: 2, images: 3, total: 5 });
    }));
    render(<Settings token="offline-token" />);
    const card = await screen.findByRole('region', { name: '队列维护' });
    expect(await within(card).findByText('文本死信：2 条')).toBeInTheDocument();
    expect(within(card).getByText('图片死信：3 条')).toBeInTheDocument();
    fireEvent.change(await screen.findByLabelText('最大会话数'), { target: { value: '30' } });
    const clear = within(card).getByRole('button', { name: '一键清除死信' });
    fireEvent.click(clear);
    expect(clear).toBeDisabled();
    fireEvent.click(clear);
    await act(async () => { complete(Response.json({ cleared: 5, text: 2, images: 3 })); });
    expect(await within(card).findByText('已清除 5 条死信。')).toBeInTheDocument();
    expect(within(card).getByText('文本死信：0 条')).toBeInTheDocument();
    expect(within(card).getByText('图片死信：0 条')).toBeInTheDocument();
    expect(clear).toBeDisabled();
    expect(screen.getByLabelText('最大会话数')).toHaveValue(30);
    expect(screen.getByRole('button', { name: '保存修改' })).toBeEnabled();
    expect(requests.filter(request => request.method === 'DELETE')).toEqual([
      { url: '/api/v1/queues/dead-letters', method: 'DELETE', authorization: 'Bearer offline-token' },
    ]);
    expect(requests.some(request => request.method === 'PATCH')).toBe(false);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it.each([
    { available: false, text: 0, images: 0, total: 0 },
    { available: true, text: 0, images: 0, total: 0 },
  ])('队列状态为 %o 时禁用清除', async status => {
    vi.stubGlobal('fetch', vi.fn(async url => Response.json(
      url === '/api/v1/settings' ? settings : status,
    )));
    render(<Settings token="" />);
    const card = await screen.findByRole('region', { name: '队列维护' });
    await within(card).findByText('文本死信：0 条');
    expect(within(card).getByRole('button', { name: '一键清除死信' })).toBeDisabled();
    if (!status.available) expect(within(card).getByText(/QQ 插件未启用或未运行/)).toBeInTheDocument();
  });

  it('删除失败显示服务端错误并允许再次清除', async () => {
    let attempts = 0;
    vi.stubGlobal('fetch', vi.fn(async (url, init) => {
      if (url === '/api/v1/settings') return Response.json(settings);
      if (init?.method === 'DELETE') {
        attempts += 1;
        return attempts === 1
          ? Response.json({ code: 'QUEUE_UNAVAILABLE', message: 'QQ 队列暂不可用' }, { status: 503 })
          : Response.json({ cleared: 1, text: 1, images: 0 });
      }
      return Response.json({ available: true, text: 1, images: 0, total: 1 });
    }));
    render(<Settings token="offline-token" />);
    const card = await screen.findByRole('region', { name: '队列维护' });
    const clear = within(card).getByRole('button', { name: '一键清除死信' });
    await waitFor(() => expect(clear).toBeEnabled());
    fireEvent.click(clear);
    expect(await within(card).findByText('QQ 队列暂不可用')).toBeInTheDocument();
    expect(clear).toBeEnabled();
    expect(within(card).getByText('文本死信：1 条')).toBeInTheDocument();
    fireEvent.click(clear);
    expect(await within(card).findByText('已清除 1 条死信。')).toBeInTheDocument();
    expect(within(card).queryByText('QQ 队列暂不可用')).not.toBeInTheDocument();
  });

  it('成功清空后显示零计数，删除条数小于旧计数时仍提示本次实际删除量', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url, init) => {
      if (url === '/api/v1/settings') return Response.json(settings);
      if (init?.method === 'DELETE') return Response.json({ cleared: 1, text: 0, images: 1 });
      return Response.json({ available: true, text: 2, images: 1, total: 3 });
    }));
    render(<QueueMaintenance token="offline-token" />);
    expect(await screen.findByText('文本死信：2 条')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '一键清除死信' }));
    expect(await screen.findByText('已清除 1 条死信。')).toBeInTheDocument();
    expect(screen.getByText('文本死信：0 条')).toBeInTheDocument();
    expect(screen.getByText('图片死信：0 条')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '一键清除死信' })).toBeDisabled();
  });

  it('读取期间禁用清除，读取失败后可刷新恢复', async () => {
    let fail!: (error: Error) => void;
    let reads = 0;
    vi.stubGlobal('fetch', vi.fn(async url => {
      if (url === '/api/v1/settings') return Response.json(settings);
      reads += 1;
      if (reads === 1) return new Promise<Response>((_resolve, reject) => { fail = reject; });
      return Response.json({ available: true, text: 1, images: 1, total: 2 });
    }));
    render(<Settings token="offline-token" />);
    const card = await screen.findByRole('region', { name: '队列维护' });
    const clear = within(card).getByRole('button', { name: '一键清除死信' });
    expect(clear).toBeDisabled();
    await act(async () => { fail(new Error('队列状态读取失败')); });
    expect(await within(card).findByText('队列状态读取失败')).toBeInTheDocument();
    expect(clear).toBeDisabled();
    fireEvent.click(within(card).getByRole('button', { name: '刷新死信数量' }));
    await waitFor(() => expect(clear).toBeEnabled());
    expect(within(card).queryByText('队列状态读取失败')).not.toBeInTheDocument();
  });
});
