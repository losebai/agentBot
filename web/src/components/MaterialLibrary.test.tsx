import { chooseSelect } from '../test-select';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { Knowledge } from '../pages/Knowledge';

const image = {id: 'a'.repeat(32), name: '旅行照片.png', kind: 'image', mime_type: 'image/png',
  size_bytes: 80, sha256: 'b'.repeat(64), description: '保留原图说明',
  created_at: '2026-10-20T00:00:00', entry: 'qq', session_id: 'qq-session',
  categories: ['received'], tags: ['旅行']};

beforeEach(() => {
  // 测试里的素材处于视口中；真实页面由观察器决定何时请求缩略图。
  vi.stubGlobal('IntersectionObserver', class {
    constructor(private callback: IntersectionObserverCallback) {}
    observe(target: Element) { this.callback([{isIntersecting: true, target} as IntersectionObserverEntry], this as unknown as IntersectionObserver); }
    disconnect() {}
  });
});

/** 仅替换 HTTP，经过实际页签、列表、详情和带认证的内容读取。 */
function setup(content?: () => Promise<Response>, count?: () => Promise<Response>, thumbnail?: () => Promise<Response>, failTags = false) {
  let current = {...image};
  URL.createObjectURL = vi.fn(() => 'blob:material');
  URL.revokeObjectURL = vi.fn();
  const fetcher = vi.fn(async (url: RequestInfo | URL, _options?: RequestInit) => {
    const path = String(url);
    if (path.endsWith('/tags') && _options?.method === 'PATCH') {
      if (failTags) return Response.json({message: '标签保存失败'}, {status: 503});
      current = {...current, tags: JSON.parse(String(_options.body)).tags};
      return Response.json(current);
    }
    if (path.endsWith('/content')) return content ? content() : new Response(new Blob(['image']));
    if (path.endsWith('/thumbnail')) return thumbnail ? thumbnail() : new Response(new Blob(['small image']));
    if (path.endsWith(`/material-library/${image.id}`)) return Response.json(current);
    if (path.includes('material-library/count')) return count ? count() : Response.json({total: 61});
    if (path.includes('material-library')) return Response.json({items: [current], next_cursor: 'next-page'});
    if (path.includes('knowledge/count')) return Response.json({total: 0});
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

it('支持页码、跳页和每页条数，筛选重置到第一页且统计匹配筛选', async () => {
  const {fetcher} = setup();
  expect(await screen.findByLabelText('分页信息')).toHaveTextContent('共 61 条 · 第 1 / 4 页');
  await waitFor(() => expect(screen.getByRole('button', {name: '下一页'})).toBeEnabled());
  fireEvent.click(screen.getByRole('button', {name: '下一页'}));
  await waitFor(() => expect(screen.getByLabelText('分页信息')).toHaveTextContent('第 2 / 4 页'));
  expect(fetcher.mock.calls.some(([url]) => String(url).includes('page=2'))).toBe(true);
  await chooseSelect(screen.getByRole('combobox', {name: '每页条数'}), '50 条/页');
  await waitFor(() => expect(screen.getByLabelText('分页信息')).toHaveTextContent('第 1 / 2 页'));
  expect(fetcher.mock.calls.some(([url]) => String(url).includes('limit=50'))).toBe(true);
  fireEvent.change(screen.getByLabelText('跳转页码'), {target: {value: '2'}});
  fireEvent.click(screen.getByRole('button', {name: '跳转'}));
  await waitFor(() => expect(screen.getByLabelText('分页信息')).toHaveTextContent('第 2 / 2 页'));
  expect(fetcher.mock.calls.filter(([url]) => String(url).includes('material-library/count'))).toHaveLength(1);
  await waitFor(() => expect(screen.getByRole('button', {name: '筛选'})).toBeEnabled());
  fireEvent.change(screen.getByLabelText('关键词'), {target: {value: '旅行'}});
  await chooseSelect(screen.getByRole('combobox', { name: '素材类型' }), '图片');
  await chooseSelect(screen.getByRole('combobox', { name: '来源' }), 'QQ');
  fireEvent.change(screen.getByLabelText('会话 ID'), {target: {value: 'qq-session'}});
  fireEvent.click(screen.getByRole('button', {name: '筛选'}));
  await waitFor(() => expect(fetcher.mock.calls.some(([url]) => String(url).includes('query='))).toBe(true));
  const path = String(fetcher.mock.calls.find(([url]) => String(url).includes('query='))?.[0]);
  expect(path).toContain('kind=image'); expect(path).toContain('entry=qq');
  expect(path).toContain('session_id=qq-session'); expect(path).not.toContain('cursor=');
  await waitFor(() => expect(screen.getByLabelText('分页信息')).toHaveTextContent('第 1 / 2 页'));
  const countPath = String(fetcher.mock.calls.filter(([url]) => String(url).includes('material-library/count')).at(-1)?.[0]);
  expect(countPath).toContain('kind=image'); expect(countPath).toContain('entry=qq');
  expect(countPath).toContain('session_id=qq-session'); expect(countPath).toContain('query=');
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
  // 迟到原件不能创建 URL；此前的列表缩略图随页签卸载释放。
  expect(URL.createObjectURL).toHaveBeenCalledTimes(1);
  expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:material');
});

it('普通 HTML 文件仅下载，不执行内联文档', async () => {
  URL.createObjectURL = vi.fn(() => 'blob:html'); URL.revokeObjectURL = vi.fn();
  vi.stubGlobal('fetch', vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    const file = {...image, kind: 'file', mime_type: 'text/html', name: '说明.html'};
    if (path.endsWith('/content')) return new Response('<script>unsafe()</script>');
    if (path.endsWith(`/material-library/${image.id}`)) return Response.json(file);
    if (path.includes('material-library/count')) return Response.json({total: 1});
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
  expect(screen.getByLabelText('普通文件')).toBeVisible();
});

it('列表使用带认证的小图接口，点击缩略图才读取原件并在翻页释放小图', async () => {
  const {fetcher} = setup();
  expect(await screen.findByRole('img', {name: '旅行照片.png缩略图'})).toHaveAttribute('src', 'blob:material');
  const thumbnail = fetcher.mock.calls.find(([url]) => String(url).endsWith('/thumbnail'));
  expect(thumbnail?.[1]?.headers).toEqual({Authorization: 'Bearer private-admin'});
  expect(String(thumbnail?.[0])).not.toContain('private-admin');
  expect(fetcher.mock.calls.some(([url]) => String(url).endsWith('/content'))).toBe(false);
  fireEvent.click(screen.getByRole('button', {name: '预览旅行照片.png'}));
  await waitFor(() => expect(screen.getByRole('link', {name: '下载原件'})).toBeVisible());
  fireEvent.click(screen.getByRole('button', {name: '关闭详情'}));
  fireEvent.click(screen.getByRole('button', {name: '下一页'}));
  await waitFor(() => expect(thumbnail?.[1]?.signal?.aborted).toBe(true));
  expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:material');
});

it('慢统计不阻塞素材列表，统计失败后提示并仍可翻页', async () => {
  let resolveCount!: (response: Response) => void;
  setup(undefined, () => new Promise(resolve => { resolveCount = resolve; }));
  expect(await screen.findByRole('button', {name: '查看旅行照片.png'})).toBeEnabled();
  expect(screen.getByLabelText('分页信息')).toHaveTextContent('正在统计总条数');
  await act(async () => resolveCount(Response.json({message: '统计暂不可用', code: 'FAILED'}, {status: 503})));
  expect(await screen.findByText('统计暂不可用')).toBeVisible();
  expect(screen.getByLabelText('分页信息')).toHaveTextContent('总条数暂不可用');
  expect(screen.getByRole('button', {name: '下一页'})).toBeEnabled();
});

it('缩略图请求在离开页签时取消，迟到正文关闭流且不创建 URL', async () => {
  let resolveThumbnail!: (response: Response) => void;
  const {fetcher} = setup(undefined, undefined, () => new Promise(resolve => { resolveThumbnail = resolve; }));
  await waitFor(() => expect(resolveThumbnail).toBeDefined());
  const request = fetcher.mock.calls.find(([url]) => String(url).endsWith('/thumbnail'));
  fireEvent.click(screen.getByRole('tab', {name: '知识条目'}));
  expect(request?.[1]?.signal?.aborted).toBe(true);
  const response = new Response(new Blob(['late thumbnail']));
  const cancel = vi.spyOn(response.body!, 'cancel');
  await act(async () => resolveThumbnail(response));
  expect(cancel).toHaveBeenCalled();
  expect(URL.createObjectURL).not.toHaveBeenCalled();
});

it('未接近可见区域的图片不读取缩略图或原件', async () => {
  vi.stubGlobal('IntersectionObserver', class {
    observe() {}
    disconnect() {}
  });
  const {fetcher} = setup();
  expect(await screen.findByRole('button', {name: '预览旅行照片.png'})).toBeEnabled();
  expect(fetcher.mock.calls.some(([url]) => /\/(thumbnail|content)$/.test(String(url)))).toBe(false);
});

it('展示来源分类和标签，按分类与标签筛选列表和总数', async () => {
  const {fetcher} = setup();
  expect(await screen.findByText('触发接收', {selector: '.ant-tag'})).toBeVisible();
  expect(screen.getByText('旅行', {selector: '.ant-tag'})).toBeVisible();
  await chooseSelect(screen.getByRole('combobox', {name: '素材分类'}), '生成图片');
  fireEvent.change(screen.getByLabelText('标签筛选'), {target: {value: '风景'}});
  fireEvent.click(screen.getByRole('button', {name: '筛选'}));
  await waitFor(() => expect(fetcher.mock.calls.filter(([url]) => String(url).includes('category=generated'))).toHaveLength(2));
  const filtered = fetcher.mock.calls.filter(([url]) => String(url).includes('category=generated'));
  expect(filtered.every(([url]) => new URL(String(url), 'http://offline').searchParams.get('tag') === '风景')).toBe(true);
});

it('编辑素材标签后刷新列表，允许删除全部标签', async () => {
  const {fetcher} = setup();
  fireEvent.click(await screen.findByLabelText('编辑旅行照片.png标签'));
  const editor = await screen.findByLabelText('素材标签');
  expect(editor).toHaveValue('旅行');
  fireEvent.change(editor, {target: {value: '风景, 假期'}});
  fireEvent.click(screen.getByText('保存标签', {selector: 'button > span'}).closest('button')!);
  await waitFor(() => expect(screen.queryByLabelText('素材标签')).not.toBeInTheDocument());
  expect(await screen.findByText('风景', {selector: '.ant-tag'})).toBeVisible();
  expect(screen.getByText('假期', {selector: '.ant-tag'})).toBeVisible();
  const write = fetcher.mock.calls.find(([, options]) => options?.method === 'PATCH');
  expect(JSON.parse(String(write?.[1]?.body))).toEqual({tags: ['风景', '假期']});
  fireEvent.click(screen.getByLabelText('编辑旅行照片.png标签'));
  fireEvent.change(await screen.findByLabelText('素材标签'), {target: {value: ''}});
  fireEvent.click(screen.getByText('保存标签', {selector: 'button > span'}).closest('button')!);
  await waitFor(() => expect(screen.queryByLabelText('素材标签')).not.toBeInTheDocument());
  expect(screen.queryByText('风景', {selector: '.ant-tag'})).not.toBeInTheDocument();
});

it('标签保存失败时保留编辑草稿并显示错误', async () => {
  setup(undefined, undefined, undefined, true);
  fireEvent.click(await screen.findByLabelText('编辑旅行照片.png标签'));
  fireEvent.change(await screen.findByLabelText('素材标签'), {target: {value: '新标签'}});
  fireEvent.click(screen.getByText('保存标签', {selector: 'button > span'}).closest('button')!);
  await waitFor(() => expect(screen.getByText('标签保存失败')).toBeVisible());
  expect(screen.getByLabelText('素材标签')).toHaveValue('新标签');
});
