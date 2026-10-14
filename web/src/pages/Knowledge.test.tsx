import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { Knowledge } from './Knowledge';

const item = {id: 1, title: '部署规则', content: '完整知识正文', tags: ['部署'], created_at: '2026-10-14T12:00:00'};

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
  await waitFor(() => expect(fetcher.mock.calls.some(([url]) => String(url).includes('cursor=page-two'))).toBe(true));
  await waitFor(() => expect(screen.getByRole('button', {name: '筛选'})).toBeEnabled());
  fireEvent.change(screen.getByLabelText('关键词'), {target: {value: '无关'}});
  fireEvent.click(screen.getByRole('button', {name: '筛选'}));
  await waitFor(() => expect(screen.queryByRole('button', {name: '查看部署规则'})).not.toBeInTheDocument());
  const query = fetcher.mock.calls.find(([url]) => String(url).includes('query='));
  expect(String(query?.[0])).not.toContain('cursor=');
});
