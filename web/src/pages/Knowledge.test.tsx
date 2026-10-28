import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { Knowledge } from './Knowledge';
import { chooseSelect } from '../test-select';

const item = {id: 1, title: '部署规则', content: '完整知识正文', tags: ['部署'], created_at: '2026-10-14T12:00:00'};

it('素材库与知识、记忆并列，只提供浏览入口', async () => {
  setup(); render(<Knowledge token="offline" />);
  fireEvent.click(screen.getByRole('tab', {name: '素材库'}));
  expect(await screen.findByRole('heading', {name: '素材库'})).toBeVisible();
  expect(screen.queryByRole('button', {name: /新增|上传|删除/})).not.toBeInTheDocument();
});

/** 只替换 HTTP 边界，页面和对话框使用真实组件。 */
function setup(fail = false) {
  let saved = false;
  const fetcher = vi.fn(async (url: RequestInfo | URL, options?: RequestInit) => {
    const path = String(url);
    if (options?.method === 'POST') {
      if (fail) return Response.json({message: '保存冲突'}, {status: 409});
      saved = true; return Response.json(item, {status: 201});
    }
    if (options?.method === 'DELETE') { saved = false; return new Response(null, {status: 204}); }
    if (path.includes('knowledge-tags')) return Response.json([{tag: '部署', count: saved ? 1 : 0}]);
    if (path.includes('/knowledge/count')) return Response.json({total: saved ? 1 : 0});
    if (path.includes('/material-library/count')) return Response.json({total: 0});
    if (path.includes('memory-tags') || path.includes('memory-scopes')) return Response.json([]);
    if (path.includes('/knowledge/')) return Response.json(item);
    return Response.json({items: saved ? [item] : [], next_cursor: null});
  });
  vi.stubGlobal('fetch', fetcher);
  return fetcher;
}

it('新增知识后可查看原文且没有编辑入口，删除须先确认', async () => {
  const fetcher = setup(); render(<Knowledge token="offline" />);
  await waitFor(() => expect(screen.getByRole('button', {name: '新增知识'})).toBeEnabled());
  fireEvent.click(screen.getByRole('button', {name: '新增知识'}));
  fireEvent.change(screen.getByLabelText('知识标题'), {target: {value: '部署规则'}});
  fireEvent.change(screen.getByLabelText('正文'), {target: {value: '完整知识正文'}});
  // 标签输入使用逗号或回车确认，测试原生键盘输入触发真实 Select。
  const tags = screen.getByRole('combobox', {name: '主题标签'});
  fireEvent.change(tags, {target: {value: '部署'}});
  fireEvent.keyDown(tags, {key: 'Enter', code: 'Enter', keyCode: 13, which: 13});
  fireEvent.click(screen.getByRole('button', {name: '保存知识'}));
  expect(await screen.findByRole('button', {name: '查看部署规则'})).toBeEnabled();
  expect(screen.queryByRole('button', {name: /编辑/})).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', {name: '查看部署规则'}));
  expect(await screen.findByText('完整知识正文')).toBeVisible();
  fireEvent.click(screen.getByRole('button', {name: '关闭详情'}));
  fireEvent.click(screen.getByRole('button', {name: '删除部署规则'}));
  expect(fetcher.mock.calls.some(([, options]) => options?.method === 'DELETE')).toBe(false);
  fireEvent.click(screen.getByRole('button', {name: '确认删除'}));
  await waitFor(() => expect(screen.queryByRole('button', {name: '查看部署规则'})).not.toBeInTheDocument());
});

it('记忆页提供新增能力，保存失败后保留正文草稿', async () => {
  setup(true); render(<Knowledge token="offline" />);
  fireEvent.click(screen.getByRole('tab', {name: '记忆'}));
  await waitFor(() => expect(screen.getByRole('button', {name: '新增记忆'})).toBeEnabled());
  fireEvent.click(screen.getByRole('button', {name: '新增记忆'}));
  fireEvent.change(screen.getByLabelText('机器人 QQ 号'), {target: {value: '10001'}});
  fireEvent.change(screen.getByLabelText('用户 QQ 号'), {target: {value: '10002'}});
  fireEvent.change(screen.getByLabelText('记忆键'), {target: {value: '偏好'}});
  fireEvent.change(screen.getByLabelText('正文'), {target: {value: '保留这段草稿'}});
  const tags = screen.getByRole('combobox', {name: '主题标签'});
  fireEvent.change(tags, {target: {value: '语言'}});
  fireEvent.keyDown(tags, {key: 'Enter', code: 'Enter', keyCode: 13, which: 13});
  fireEvent.click(screen.getByRole('button', {name: '保存记忆'}));
  await waitFor(() => expect(screen.getByText('保存冲突')).toBeVisible());
  expect(screen.getByLabelText('正文')).toHaveValue('保留这段草稿');
});

it('关闭详情后迟到响应不会重开弹窗，筛选从第一页重新查询', async () => {
  let resolveDetail: (response: Response) => void = () => undefined;
  const fetcher = vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.includes('knowledge-tags')) return Response.json([{tag: '部署', count: 1}]);
    if (path.includes('/knowledge/count')) return Response.json({total: path.includes('query=') ? 0 : 41});
    if (path.endsWith('/knowledge/1')) return new Promise<Response>(resolve => { resolveDetail = resolve; });
    if (path.includes('query=')) return Response.json({items: [], next_cursor: null});
    return Response.json({items: [item], next_cursor: 'page-two'});
  });
  vi.stubGlobal('fetch', fetcher); render(<Knowledge token="offline" />);
  fireEvent.click(await screen.findByRole('button', {name: '查看部署规则'}));
  fireEvent.click(screen.getByRole('button', {name: '关闭详情'}));
  await act(async () => { resolveDetail(Response.json(item)); });
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', {name: '下一页'}));
  await waitFor(() => expect(fetcher.mock.calls.some(([url]) => String(url).includes('page=2'))).toBe(true));
  await waitFor(() => expect(screen.getByRole('button', {name: '筛选'})).toBeEnabled());
  fireEvent.change(screen.getByLabelText('关键词'), {target: {value: '无关'}});
  fireEvent.click(screen.getByRole('button', {name: '筛选'}));
  await waitFor(() => expect(screen.queryByRole('button', {name: '查看部署规则'})).not.toBeInTheDocument());
  const query = fetcher.mock.calls.find(([url]) => String(url).includes('query='));
  expect(String(query?.[0])).not.toContain('cursor=');
});

it('知识直接跳页及每页大小复用总数与标签目录，筛选和删除回首页重新统计', async () => {
  const requests: URL[] = [];
  let deleted = false;
  vi.stubGlobal('fetch', vi.fn(async (url, options) => {
    const request = new URL(String(url), 'http://offline'); requests.push(request);
    if (options?.method === 'DELETE') { deleted = true; return new Response(null, {status: 204}); }
    if (request.pathname.endsWith('/knowledge-tags')) return Response.json([{tag: '部署', count: 101}]);
    if (request.pathname.endsWith('/count')) return Response.json({total: deleted ? 100 : 101});
    const number = Number(request.searchParams.get('page') || 1);
    return Response.json({items: [{...item, title: `部署规则${number}`}], next_cursor: 'more'});
  }));
  render(<Knowledge token="offline" />);
  expect(await screen.findByText(/共 101 条 · 第 1 \/ 6 页/)).toBeVisible();
  await waitFor(() => expect(screen.getByRole('button', {name: '跳转'})).toBeEnabled());
  fireEvent.change(screen.getByRole('spinbutton', {name: '跳转页码'}), {target: {value: '6'}});
  fireEvent.click(screen.getByRole('button', {name: '跳转'}));
  expect(await screen.findByRole('button', {name: '查看部署规则6'})).toBeEnabled();
  expect(requests.filter(request => request.pathname.endsWith('/count'))).toHaveLength(1);
  expect(requests.filter(request => request.pathname.endsWith('/knowledge-tags'))).toHaveLength(1);
  await chooseSelect(screen.getByRole('combobox', {name: '每页条数'}), '50 条/页');
  await waitFor(() => expect(screen.getByText(/共 101 条 · 第 1 \/ 3 页/)).toBeVisible());
  await chooseSelect(screen.getByRole('combobox', {name: '每页条数'}), '100 条/页');
  await waitFor(() => expect(screen.getByText(/共 101 条 · 第 1 \/ 2 页/)).toBeVisible());
  expect(requests.filter(request => request.pathname.endsWith('/count'))).toHaveLength(1);
  fireEvent.click(screen.getByRole('button', {name: '下一页'}));
  expect(await screen.findByRole('button', {name: '查看部署规则2'})).toBeEnabled();
  fireEvent.change(screen.getByLabelText('关键词'), {target: {value: '部署'}});
  await chooseSelect(screen.getByRole('combobox', {name: '标签'}), '部署（101）');
  fireEvent.click(screen.getByRole('button', {name: '筛选'}));
  expect(await screen.findByRole('button', {name: '查看部署规则1'})).toBeEnabled();
  expect(requests.filter(request => request.pathname.endsWith('/count'))).toHaveLength(2);
  expect(requests.at(-1)?.searchParams.get('query')).toBe('部署');
  expect(requests.at(-1)?.searchParams.get('tag')).toBe('部署');
  fireEvent.click(screen.getByRole('button', {name: '下一页'}));
  fireEvent.click(await screen.findByRole('button', {name: '删除部署规则2'}));
  fireEvent.click(screen.getByRole('button', {name: '确认删除'}));
  expect(await screen.findByText(/共 100 条 · 第 1 \/ 1 页/)).toBeVisible();
  expect(await screen.findByRole('button', {name: '查看部署规则1'})).toBeEnabled();
  expect(requests.filter(request => request.pathname.endsWith('/count'))).toHaveLength(3);
  expect(requests.filter(request => request.pathname.endsWith('/knowledge-tags'))).toHaveLength(2);
});

it('知识标签目录及统计迟到时首屏仍可操作，旧统计不覆盖筛选结果', async () => {
  let resolveTags!: (response: Response) => void, resolveCount!: (response: Response) => void;
  let countSignal: AbortSignal | null | undefined;
  vi.stubGlobal('fetch', vi.fn(async (url, options) => {
    const request = new URL(String(url), 'http://offline');
    if (request.pathname.endsWith('/knowledge-tags')) return new Promise<Response>(resolve => { resolveTags = resolve; });
    if (request.pathname.endsWith('/count')) {
      if (request.searchParams.has('query')) return Response.json({total: 0});
      countSignal = options?.signal;
      return new Promise<Response>(resolve => { resolveCount = resolve; });
    }
    return Response.json({items: request.searchParams.has('query') ? [] : [item], next_cursor: null});
  }));
  render(<Knowledge token="offline" />);
  expect(await screen.findByRole('button', {name: '查看部署规则'})).toBeEnabled();
  fireEvent.change(screen.getByLabelText('关键词'), {target: {value: '无关'}});
  fireEvent.click(screen.getByRole('button', {name: '筛选'}));
  expect(await screen.findByText(/共 0 条/)).toBeVisible();
  expect(countSignal?.aborted).toBe(true);
  await act(async () => { resolveCount(Response.json({total: 999})); resolveTags(Response.json([])); });
  expect(screen.getByText(/共 0 条/)).toBeVisible();
  expect(screen.queryByRole('button', {name: '查看部署规则'})).not.toBeInTheDocument();
});
