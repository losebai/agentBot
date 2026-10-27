import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { MediaApi } from '../media-api';
import { VideoUpload } from './VideoUpload';

afterEach(() => vi.unstubAllGlobals());

it('视频使用独立上传入口及当前会话，返回资产后才允许发送', async () => {
  const asset = { id: 'a'.repeat(32), mime_type: 'video/mp4' };
  const fetcher = vi.fn(async (_url: RequestInfo | URL) => Response.json(asset)); vi.stubGlobal('fetch', fetcher);
  const changed = vi.fn(), busy = vi.fn();
  render(<VideoUpload api={new MediaApi('offline')} sessionId="current" disabled={false}
    onChange={changed} onBusyChange={busy} />);
  fireEvent.change(screen.getByLabelText('添加投稿视频'), {
    target: { files: [new File(['video'], '投稿.mp4', { type: 'video/mp4' })] },
  });
  expect(await screen.findByText('投稿.mp4 · 已上传')).toBeVisible();
  expect(fetcher.mock.calls[0][0]).toContain('/media/video/uploads?session_id=current');
  expect(changed).toHaveBeenLastCalledWith(asset);
  expect(busy.mock.calls.map(([value]) => value)).toEqual([true, false]);
});

it('非法格式不发起上传，切换会话取消旧视频请求', async () => {
  let signal: AbortSignal | undefined;
  vi.stubGlobal('fetch', vi.fn((url, init) => { signal = init.signal; return new Promise(() => {}); }));
  const view = render(<VideoUpload api={new MediaApi('offline')} sessionId="old" disabled={false}
    onChange={vi.fn()} onBusyChange={vi.fn()} />);
  fireEvent.change(screen.getByLabelText('添加投稿视频'), {
    target: { files: [new File(['text'], '伪视频.txt', { type: 'text/plain' })] },
  });
  expect(await screen.findByText('请选择不超过 100 MB 的 MP4 视频。')).toBeVisible();
  expect(signal).toBeUndefined();
  fireEvent.change(screen.getByLabelText('添加投稿视频'), {
    target: { files: [new File(['video'], 'a.mp4', { type: 'video/mp4' })] },
  });
  view.unmount(); expect(signal?.aborted).toBe(true);
});
