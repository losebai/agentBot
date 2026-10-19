import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { Knowledge } from '../pages/Knowledge';

const image = {id: 'a'.repeat(32), name: '旅行照片.png', kind: 'image', mime_type: 'image/png',
  size_bytes: 80, sha256: 'b'.repeat(64), description: '保留原图说明',
  created_at: '2026-10-20T00:00:00', entry: 'qq', session_id: 'qq-session'};

/** 仅替换 HTTP，经过实际页签、列表、详情和带认证的内容读取。 */
function setup(content?: () => Promise<Response>) {
  URL.createObjectURL = vi.fn(() => 'blob:material');
  URL.revokeObjectURL = vi.fn();
  const fetcher = vi.fn(async (url: RequestInfo | URL, _options?: RequestInit) => {
    const path = String(url);
    if (path.endsWith('/content')) return content ? content() : new Response(new Blob(['image']));
    if (path.endsWith(`/material-library/${image.id}`)) return Response.json(image);
    if (path.includes('material-library')) return Response.json({items: [image], next_cursor: path.includes('cursor=') ? null : 'next-page'});
    if (path.includes('knowledge-tags')) return Response.json([]);
    return Response.json({items: [], next_cursor: null});
  });
  vi.stubGlobal('fetch', fetcher);
  const view = render(<Knowledge token="private-admin" />);
  fireEvent.click(screen.getByRole('tab', {name: '素材库'}));
  return {fetcher, view};
}

it('从 QQ 素材列表查看原件，下载只使用本地 URL 且关闭撤销', async () => {
  const {fetcher} = setup();
  fireEvent.click(await screen.findByRole('button', {name: '查看旅行照片.png'}));
  expect(await screen.findByRole('img', {name: '旅行照片.png'})).toHaveAttribute('src', 'blob:material');
  expect(screen.getByRole('link', {name: '下载原件'})).toHaveAttribute('download', '旅行照片.png');
  expect(screen.getByRole('link', {name: '下载原件'})).toHaveAttribute('href', 'blob:material');
  await waitFor(() => expect(within(screen.getByRole('dialog')).getByText('保留原图说明')).toBeVisible());
  const request = fetcher.mock.calls.find(([url]) => String(url).endsWith('/content'));
  expect(request?.[1]?.headers).toEqual({Authorization: 'Bearer private-admin'});
  expect(String(request?.[0])).not.toContain('private-admin');
  fireEvent.click(screen.getByRole('button', {name: '关闭详情'}));
  expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:material');
});

it('筛选来源、会话和关键词时清空翻页游标', async () => {
  const {fetcher} = setup();
  await waitFor(() => expect(screen.getByRole('button', {name: '下一页'})).toBeEnabled());
  fireEvent.click(screen.getByRole('button', {name: '下一页'}));
  await waitFor(() => expect(fetcher.mock.calls.some(([url]) => String(url).includes('cursor=next-page'))).toBe(true));
  await waitFor(() => expect(screen.getByRole('button', {name: '筛选'})).toBeEnabled());
  fireEvent.change(screen.getByLabelText('关键词'), {target: {value: '旅行'}});
  fireEvent.change(screen.getByLabelText('素材类型'), {target: {value: 'image'}});
  fireEvent.change(screen.getByLabelText('来源'), {target: {value: 'qq'}});
  fireEvent.change(screen.getByLabelText('会话 ID'), {target: {value: 'qq-session'}});
  fireEvent.click(screen.getByRole('button', {name: '筛选'}));
  await waitFor(() => expect(fetcher.mock.calls.some(([url]) => String(url).includes('query='))).toBe(true));
  const path = String(fetcher.mock.calls.find(([url]) => String(url).includes('query='))?.[0]);
  expect(path).toContain('kind=image'); expect(path).toContain('entry=qq');
  expect(path).toContain('session_id=qq-session'); expect(path).not.toContain('cursor=');
});

it('切换页签中止内容读取，迟到响应关闭流且不再显示详情', async () => {
  let resolveContent!: (response: Response) => void;
  const {fetcher} = setup(() => new Promise(resolve => { resolveContent = resolve; }));
  fireEvent.click(await screen.findByRole('button', {name: '查看旅行照片.png'}));
  await waitFor(() => expect(resolveContent).toBeDefined());
  const request = fetcher.mock.calls.find(([url]) => String(url).endsWith('/content'));
  fireEvent.click(screen.getByRole('tab', {name: '知识条目'}));
  expect(request?.[1]?.signal?.aborted).toBe(true);
  const late = new Response(new Blob(['late']));
  const cancel = vi.spyOn(late.body!, 'cancel');
  await act(async () => { resolveContent(late); });
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(cancel).toHaveBeenCalled();
  expect(URL.createObjectURL).not.toHaveBeenCalled();
});

it('普通 HTML 文件仅下载，不执行内联文档', async () => {
  URL.createObjectURL = vi.fn(() => 'blob:html'); URL.revokeObjectURL = vi.fn();
  vi.stubGlobal('fetch', vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    const file = {...image, kind: 'file', mime_type: 'text/html', name: '说明.html'};
    if (path.endsWith('/content')) return new Response('<script>unsafe()</script>');
    if (path.endsWith(`/material-library/${image.id}`)) return Response.json(file);
    if (path.includes('material-library')) return Response.json({items: [file], next_cursor: null});
    if (path.includes('knowledge-tags')) return Response.json([]);
    return Response.json({items: [], next_cursor: null});
  }));
  render(<Knowledge token="private-admin" />);
  fireEvent.click(screen.getByRole('tab', {name: '素材库'}));
  fireEvent.click(await screen.findByRole('button', {name: '查看说明.html'}));
  expect(await screen.findByRole('link', {name: '下载原件'})).toHaveAttribute('href', 'blob:html');
  expect(document.querySelector('.media-attachment img')).toBeNull();
  expect(document.querySelector('iframe,object,embed')).toBeNull();
});
