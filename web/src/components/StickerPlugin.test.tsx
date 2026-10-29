import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { StickerPlugin } from './StickerPlugin';
import type { StickerView } from '../sticker-types';

const sticker: StickerView = {
  id: 'a'.repeat(32), name: '开心', tags: ['猫猫'], description: '猫在笑', emotion: '开心',
  usage_hint: '开心的时候', enabled: true, revision: 3, mime_type: 'image/gif', size_bytes: 100,
  sha256: 'hash', width: 32, height: 32, frame_count: 2, animated: true,
  analysis_status: 'ready', analysis_error: null, created_at: '2026-10-29', updated_at: '2026-10-29',
};

/** 模拟同源认证接口，上传和编辑测试不依赖真实模型及数据库。 */
function serve(options: { conflict?: boolean; uploaded?: StickerView; content?: () => Promise<Response>;
  upload?: (url: string, init?: RequestInit) => Promise<Response> } = {}) {
  const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.includes('/content?')) return options.content ? options.content() : new Response(new Blob(['image'], { type: 'image/png' }));
    if (url.includes('/uploads?')) return options.upload ? options.upload(url, init) : Response.json(options.uploaded || sticker);
    if (init?.method === 'PATCH') return options.conflict
      ? Response.json({ message: '表情包版本冲突，请重新加载', code: 'CONFLICT' }, { status: 409 })
      : Response.json({ ...sticker, ...JSON.parse(String(init.body)), revision: 4 });
    if (url.endsWith('/stickers/search')) return Response.json({ matches: [{ sticker, score: 0.9, reason: '适合开心的聊天语境' }] });
    if (init?.method === 'DELETE') return Response.json({ deleted: true });
    return Response.json({ items: [sticker], total: 1, next_offset: null });
  });
  vi.stubGlobal('fetch', fetcher);
  vi.stubGlobal('IntersectionObserver', class {
    private disconnected = false;
    constructor(private callback: (entries: { isIntersecting: boolean }[]) => void) {}
    observe() { queueMicrotask(() => { if (!this.disconnected) this.callback([{ isIntersecting: true }]); }); }
    disconnect() { this.disconnected = true; }
  });
  vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: vi.fn(() => 'blob:sticker'), revokeObjectURL: vi.fn() }));
  return fetcher;
}

it('原始GIF上传携带认证、自定义标签，识图失败仍显示已保存状态', async () => {
  const fetcher = serve({ uploaded: { ...sticker, analysis_status: 'failed', analysis_error: '模型请求超时' } });
  render(<StickerPlugin token="offline" />);
  fireEvent.click(await screen.findByRole('button', { name: '添加表情包' }));
  const file = new File(['GIF89a'], 'cat.gif', { type: 'image/gif' });
  fireEvent.change(screen.getByLabelText('图片文件'), { target: { files: [file] } });
  fireEvent.change(screen.getByLabelText('上传名称'), { target: { value: '猫图' } });
  fireEvent.change(screen.getByLabelText('上传标签'), { target: { value: '猫猫，开心\n猫猫' } });
  fireEvent.click(screen.getByRole('button', { name: '开始上传' }));
  // 接口替身立即返回，需等待弹窗入场动画结束后检查可见结果。
  await waitFor(() => expect(screen.getByText(/图片已保存.*模型请求超时/)).toBeVisible());
  const [url, init] = fetcher.mock.calls.find(([url]) => String(url).includes('/uploads?'))!;
  expect(new URL(String(url), 'http://localhost').searchParams.get('tags')).toBe('["猫猫","开心"]');
  expect(init).toMatchObject({ method: 'POST', body: file, headers: { Authorization: 'Bearer offline', 'Content-Type': 'image/gif' } });
});

it('语境检索位于表情列表上方', async () => {
  serve();
  render(<StickerPlugin token="offline" />);
  const list = (await screen.findByLabelText('编辑开心')).closest('.sticker-grid')!;
  const search = screen.getByLabelText('聊天语境').closest('form')!;
  expect(search.compareDocumentPosition(list) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
});

it('弹窗支持拖放多张图片、关闭自动识图和只重试失败项', async () => {
  let failFirst = true;
  const fetcher = serve({ upload: async (url, init) => {
    if ((init?.body as File).name === 'bad.gif' && failFirst) {
      failFirst = false;
      return Response.json({ message: '图片上传失败', code: 'UPLOAD_FAILED' }, { status: 422 });
    }
    return Response.json({ ...sticker, name: new URL(url, 'http://localhost').searchParams.get('name') });
  } });
  render(<StickerPlugin token="offline" />);
  expect(screen.queryByLabelText('图片文件')).not.toBeInTheDocument();
  fireEvent.click(screen.getByText('添加表情包', { selector: 'button > span' }));
  expect(screen.getByLabelText('图片文件')).toHaveAttribute('multiple');
  fireEvent.drop(screen.getByLabelText('拖拽上传区域'), { dataTransfer: { files: [
    new File(['GIF89a'], 'bad.gif', { type: 'image/gif' }),
    new File(['PNG'], 'good.png', { type: 'image/png' }),
  ] } });
  fireEvent.click(screen.getByLabelText('上传后自动识图'));
  fireEvent.click(screen.getByRole('button', { name: '开始上传' }));
  await waitFor(() => expect(screen.getByText('图片上传失败')).toBeVisible());
  await waitFor(() => expect(screen.getByRole('button', { name: '重试失败项' })).toBeEnabled());
  expect(fetcher.mock.calls.filter(([url]) => String(url).includes('/uploads?'))).toHaveLength(2);
  fireEvent.click(screen.getByRole('button', { name: '重试失败项' }));
  await waitFor(() => expect(fetcher.mock.calls.filter(([url]) => String(url).includes('/uploads?'))).toHaveLength(3));
  const uploads = fetcher.mock.calls.filter(([url]) => String(url).includes('/uploads?'));
  expect(uploads.map(([, init]) => (init?.body as File).name)).toEqual(['bad.gif', 'good.png', 'bad.gif']);
  expect(uploads.every(([url]) => new URL(String(url), 'http://localhost').searchParams.get('auto_analyze') === 'false')).toBe(true);
});

it('拖动弹窗标题会改变位置，关闭弹窗后停止剩余上传', async () => {
  let finish!: (value: Response) => void;
  const fetcher = serve({ upload: () => new Promise(resolve => { finish = resolve; }) });
  render(<StickerPlugin token="offline" />);
  fireEvent.click(screen.getByText('添加表情包', { selector: 'button > span' }));
  const title = screen.getByText('添加表情包', { selector: '.sticker-upload-title' });
  fireEvent.mouseDown(title, { button: 0, clientX: 100, clientY: 100 });
  fireEvent.mouseMove(window, { clientX: 160, clientY: 140 });
  fireEvent.mouseUp(window);
  expect(title.closest('.sticker-upload-position')).toHaveStyle({ transform: 'translate(60px, 40px)' });
  fireEvent.change(screen.getByLabelText('图片文件'), { target: { files: [
    new File(['a'], 'first.png', { type: 'image/png' }), new File(['b'], 'second.png', { type: 'image/png' }),
  ] } });
  fireEvent.click(screen.getByRole('button', { name: '开始上传' }));
  await waitFor(() => expect(fetcher.mock.calls.some(([url]) => String(url).includes('/uploads?'))).toBe(true));
  const request = fetcher.mock.calls.find(([url]) => String(url).includes('/uploads?'))![1]!;
  fireEvent.click(screen.getByRole('button', { name: '停止并关闭' }));
  expect(request.signal?.aborted).toBe(true);
  await act(async () => { finish(Response.json(sticker)); });
  expect(fetcher.mock.calls.filter(([url]) => String(url).includes('/uploads?'))).toHaveLength(1);
});

it('只提交实际修改的标签和旧版本，冲突后保留管理员草稿', async () => {
  const fetcher = serve({ conflict: true });
  render(<StickerPlugin token="offline" />);
  fireEvent.click(await screen.findByRole('button', { name: '编辑开心' }));
  fireEvent.change(screen.getByLabelText('表情标签'), { target: { value: '新标签' } });
  fireEvent.click(screen.getByRole('button', { name: '保存表情包' }));
  await waitFor(() => expect(screen.getByText('表情包版本冲突，请重新加载')).toBeVisible());
  expect(screen.getByLabelText('表情标签')).toHaveValue('新标签');
  const [, init] = fetcher.mock.calls.find(([, init]) => init?.method === 'PATCH')!;
  expect(JSON.parse(String(init?.body))).toEqual({ expected_revision: 3, tags: ['新标签'] });
});

it('预览通过认证读取，关闭详情和卸载取消请求并撤销原件及缩略图URL', async () => {
  const fetcher = serve();
  const view = render(<StickerPlugin token="offline" />);
  await screen.findByAltText('开心预览');
  fireEvent.click(screen.getByRole('button', { name: '编辑开心' }));
  await screen.findByAltText('开心原件');
  const contentCalls = fetcher.mock.calls.filter(([url]) => String(url).includes('/content?'));
  expect(contentCalls).toHaveLength(2);
  expect(contentCalls[0][1]?.headers).toEqual({ Authorization: 'Bearer offline' });
  expect(String(contentCalls[1][0])).toContain('preview=false');
  fireEvent.click(screen.getByRole('button', { name: /取\s*消/ }));
  await waitFor(() => expect((contentCalls[1][1]?.signal as AbortSignal).aborted).toBe(true));
  view.unmount();
  expect((contentCalls[0][1]?.signal as AbortSignal).aborted).toBe(true);
  expect(URL.revokeObjectURL).toHaveBeenCalledTimes(2);
});

it('停用只提交启用状态，清空自定义标签显式提交空数组', async () => {
  const fetcher = serve();
  render(<StickerPlugin token="offline" />);
  fireEvent.click(await screen.findByRole('button', { name: '编辑开心' }));
  fireEvent.change(screen.getByLabelText('表情标签'), { target: { value: '' } });
  fireEvent.click(screen.getByLabelText('允许机器人检索和发送'));
  fireEvent.click(screen.getByRole('button', { name: '保存表情包' }));
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  const [, init] = fetcher.mock.calls.find(([, init]) => init?.method === 'PATCH')!;
  expect(JSON.parse(String(init?.body))).toEqual({ expected_revision: 3, tags: [], enabled: false });
});

it('图片读取迟于卸载时不创建对象URL', async () => {
  let finish!: (value: Response) => void;
  const fetcher = serve({ content: () => new Promise(resolve => { finish = resolve; }) });
  const view = render(<StickerPlugin token="offline" />);
  await waitFor(() => expect(fetcher.mock.calls.some(([url]) => String(url).includes('/content?'))).toBe(true));
  view.unmount();
  await act(async () => { finish(new Response(new Blob(['late image']))); });
  expect(URL.createObjectURL).not.toHaveBeenCalled();
});

it('语境试用提交自定义标签和上下文，仅展示匹配理由而不发送图片', async () => {
  const fetcher = serve();
  render(<StickerPlugin token="offline" />);
  fireEvent.change(screen.getByLabelText('聊天语境'), { target: { value: '今天终于上线了！' } });
  fireEvent.change(screen.getByLabelText('限定标签'), { target: { value: '开心，猫猫' } });
  fireEvent.click(screen.getByRole('button', { name: '检索表情包' }));
  expect(await screen.findByText('适合开心的聊天语境')).toBeVisible();
  const [, init] = fetcher.mock.calls.find(([url]) => String(url).endsWith('/stickers/search'))!;
  expect(JSON.parse(String(init?.body))).toEqual({ context: '今天终于上线了！', tags: ['开心', '猫猫'], limit: 5 });
  expect(fetcher.mock.calls.some(([url]) => String(url).includes('/send'))).toBe(false);
});
