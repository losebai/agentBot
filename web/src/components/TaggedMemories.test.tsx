import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { TaggedMemories } from './TaggedMemories';

const memory = {id: 1, scope_id: 'scope-user', scope: 'user', scope_title: '小明 (30001)',
  key: '网关限制', content: '413 来自网关请求体限制', tags: ['部署'], topic: '413 网关报错',
  kind: 'fact', revision: 3, status: 'active', effective_status: 'active', source_kind: 'legacy_unknown',
  subject_type: 'user', subject_id: 'subject-one', importance: 0.6, observed_at: null,
  valid_from: null, valid_until: null, last_used_at: null, created_at: '2026-10-20T01:00:00Z',
  updated_at: '2026-10-20T02:00:00Z', sources: []};

/** 仅隔离 HTTP，真实页面负责表单、版本检查和确认步骤。 */
function setup(handler?: (path: string, options?: RequestInit) => Response | Promise<Response> | undefined) {
  const fetcher = vi.fn(async (url: RequestInfo | URL, options?: RequestInit) => {
    const path = String(url);
    const response = await handler?.(path, options);
    if (response) return response;
    if (path.includes('memory-scopes')) return Response.json([{scope_id: 'scope-user', scope: 'user', scope_title: '小明 (30001)', count: 1}]);
    if (path.includes('memory-tags')) return Response.json([{tag: '部署', count: 1}]);
    if (path.endsWith('/memories/1')) return Response.json(memory);
    if (path.includes('/revisions')) return Response.json({items: [], next_cursor: null});
    return Response.json({items: [memory], next_cursor: null});
  });
  vi.stubGlobal('fetch', fetcher);
  return fetcher;
}

it('编辑只提交改动字段，版本冲突保留草稿且不自动覆盖', async () => {
  const fetcher = setup((_path, options) => options?.method === 'PATCH'
    ? Response.json({message: '记忆已被其他操作更新'}, {status: 409}) : undefined);
  render(<TaggedMemories token="offline" />);
  fireEvent.click(await screen.findByRole('button', {name: '编辑网关限制'}));
  const content = await screen.findByLabelText('正文');
  fireEvent.change(content, {target: {value: '413 已确认来自反向代理'}});
  fireEvent.change(screen.getByLabelText('修改原因'), {target: {value: '根据日志更正'}});
  fireEvent.click(screen.getByRole('button', {name: '保存修改'}));
  expect(await screen.findByText(/记忆已被其他操作更新/)).toBeVisible();
  expect(content).toHaveValue('413 已确认来自反向代理');
  const writes = fetcher.mock.calls.filter(([, options]) => options?.method === 'PATCH');
  expect(writes).toHaveLength(1);
  expect(JSON.parse(String(writes[0][1]?.body))).toEqual({expected_revision: 3,
    content: '413 已确认来自反向代理', reason: '根据日志更正'});
});

it('详情区分未知来源并显示历史版本和事实有效期', async () => {
  setup(path => path.includes('/revisions') ? Response.json({items: [{id: 8, memory_id: 1, revision: 2,
    operation: 'revise', snapshot: {...memory, revision: 2, content: '旧的网关结论'}, reason: '更正初步判断',
    created_at: '2026-10-19T02:00:00Z'}], next_cursor: null}) : undefined);
  render(<TaggedMemories token="offline" />);
  fireEvent.click(await screen.findByRole('button', {name: '查看网关限制'}));
  await waitFor(() => expect(screen.getByText('来源未知，不能视为本人已确认。')).toBeVisible());
  expect(screen.getByText('事实时间：未知')).toBeVisible();
  fireEvent.click(screen.getByRole('button', {name: '版本历史'}));
  expect(await screen.findByText('旧的网关结论')).toBeVisible();
  expect(screen.getByText('更正初步判断')).toBeVisible();
});

it('清理只提交预览中的版本，确认前不发送清理写入', async () => {
  const fetcher = setup((path, options) => {
    if (path.endsWith('/cleanup-preview')) return Response.json({items: [memory], digest: 'preview-one', action: 'expire'});
    if (path.endsWith('/cleanup') && options?.method === 'POST') return Response.json({items: []});
    return undefined;
  });
  render(<TaggedMemories token="offline" />);
  await screen.findByRole('button', {name: '查看网关限制'});
  fireEvent.change(screen.getByLabelText('目标作用域'), {target: {value: 'scope-user'}});
  fireEvent.change(screen.getByLabelText('主题'), {target: {value: '413 网关报错'}});
  fireEvent.click(screen.getByRole('button', {name: '预览主题清理'}));
  const dialog = await screen.findByRole('dialog', {name: '主题清理预览'});
  await waitFor(() => expect(within(dialog).getByText('413 来自网关请求体限制')).toBeVisible());
  expect(fetcher.mock.calls.filter(([url]) => String(url).endsWith('/cleanup'))).toHaveLength(0);
  fireEvent.change(within(dialog).getByLabelText('操作原因'), {target: {value: '旧环境已下线'}});
  fireEvent.click(within(dialog).getByRole('button', {name: '确认清理'}));
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  const write = fetcher.mock.calls.find(([url]) => String(url).endsWith('/cleanup'));
  expect(JSON.parse(String(write?.[1]?.body))).toEqual({items: [{id: 1, expected_revision: 3}],
    action: 'expire', digest: 'preview-one', reason: '旧环境已下线'});
});

it('合并先预览且使用预览版本与摘要提交', async () => {
  const second = {...memory, id: 2, key: '网关重复', revision: 1};
  const fetcher = setup((path, options) => {
    if (path.includes('/memories?')) return Response.json({items: [memory, second], next_cursor: null});
    if (path.endsWith('/merge-preview')) return Response.json({items: [memory, second], digest: 'merge-one', action: 'merge', target_id: 1});
    if (path.endsWith('/merge') && options?.method === 'POST') return Response.json(memory);
    return undefined;
  });
  render(<TaggedMemories token="offline" />);
  fireEvent.click(await screen.findByRole('checkbox', {name: '选择网关限制'}));
  fireEvent.click(screen.getByRole('checkbox', {name: '选择网关重复'}));
  fireEvent.click(screen.getByRole('button', {name: '合并所选记忆'}));
  fireEvent.change(screen.getByLabelText('操作原因'), {target: {value: '同一主体的重复事实'}});
  fireEvent.click(screen.getByRole('button', {name: '预览合并'}));
  await screen.findByRole('button', {name: '确认合并'});
  expect(fetcher.mock.calls.some(([url]) => String(url).endsWith('/merge'))).toBe(false);
  fireEvent.click(screen.getByRole('button', {name: '确认合并'}));
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  const write = fetcher.mock.calls.find(([url]) => String(url).endsWith('/merge'));
  expect(JSON.parse(String(write?.[1]?.body))).toEqual({target_id: 1,
    items: [{id: 1, expected_revision: 3}, {id: 2, expected_revision: 1}],
    digest: 'merge-one', reason: '同一主体的重复事实'});
});

it('遗忘携带列表版本且须明确确认', async () => {
  const fetcher = setup((_path, options) => options?.method === 'DELETE' ? new Response(null, {status: 204}) : undefined);
  render(<TaggedMemories token="offline" />);
  fireEvent.click(await screen.findByRole('button', {name: '遗忘网关限制'}));
  expect(screen.getByText(/仍保留管理员审计历史/)).toBeInTheDocument();
  expect(fetcher.mock.calls.some(([, options]) => options?.method === 'DELETE')).toBe(false);
  fireEvent.click(screen.getByRole('button', {name: '确认遗忘'}));
  await waitFor(() => expect(fetcher.mock.calls.some(([url, options]) =>
    String(url).endsWith('/memories/1?expected_revision=3') && options?.method === 'DELETE')).toBe(true));
});

it('关闭详情取消读取并阻止迟到正文重开弹窗', async () => {
  let finish: (response: Response) => void = () => undefined;
  const fetcher = setup(path => path.endsWith('/memories/1')
    ? new Promise<Response>(resolve => { finish = resolve; }) : undefined);
  render(<TaggedMemories token="offline" />);
  fireEvent.click(await screen.findByRole('button', {name: '查看网关限制'}));
  fireEvent.click(screen.getByRole('button', {name: '关闭详情'}));
  const read = fetcher.mock.calls.find(([url]) => String(url).endsWith('/memories/1'));
  expect(read?.[1]?.signal?.aborted).toBe(true);
  await act(async () => { finish(Response.json(memory)); });
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});

it('清理预览仍可取消，关闭后中止未完成的读取', async () => {
  let finish: (response: Response) => void = () => undefined;
  const fetcher = setup(path => path.endsWith('/cleanup-preview')
    ? new Promise<Response>(resolve => { finish = resolve; }) : undefined);
  render(<TaggedMemories token="offline" />);
  await screen.findByRole('button', {name: '查看网关限制'});
  fireEvent.change(screen.getByLabelText('目标作用域'), {target: {value: 'scope-user'}});
  fireEvent.change(screen.getByLabelText('主题'), {target: {value: '413 网关报错'}});
  fireEvent.click(screen.getByRole('button', {name: '预览主题清理'}));
  const cancel = await screen.findByRole('button', {name: /取\s*消/});
  expect(cancel).toBeEnabled();
  fireEvent.click(cancel);
  const request = fetcher.mock.calls.find(([url]) => String(url).endsWith('/cleanup-preview'));
  expect(request?.[1]?.signal?.aborted).toBe(true);
  await act(async () => { finish(Response.json({items: [memory], digest: 'late', action: 'expire'})); });
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});

it('新增省略未填写的可选主题，不发送会被校验拒绝的空主题', async () => {
  const fetcher = setup((_path, options) => options?.method === 'POST' ? Response.json(memory, {status: 201}) : undefined);
  render(<TaggedMemories token="offline" />);
  await waitFor(() => expect(screen.getByRole('button', {name: '新增记忆'})).toBeEnabled());
  fireEvent.click(screen.getByRole('button', {name: '新增记忆'}));
  fireEvent.change(screen.getByLabelText('记忆键'), {target: {value: '写作偏好'}});
  fireEvent.change(screen.getByLabelText('正文'), {target: {value: '习惯用中文写作'}});
  const tags = screen.getByRole('combobox', {name: '主题标签'});
  fireEvent.change(tags, {target: {value: '写作'}});
  fireEvent.keyDown(tags, {key: 'Enter', code: 'Enter', keyCode: 13, which: 13});
  fireEvent.click(screen.getByRole('button', {name: '保存记忆'}));
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  const write = fetcher.mock.calls.find(([, options]) => options?.method === 'POST');
  expect(JSON.parse(String(write?.[1]?.body))).not.toHaveProperty('topic');
});

it('旧时间不假定 UTC，详情明确提示创建时间时区未知', async () => {
  setup(path => path.endsWith('/memories/1') ? Response.json({...memory,
    created_time_basis: 'legacy_unspecified', updated_time_basis: 'utc'}) : undefined);
  render(<TaggedMemories token="offline" />);
  fireEvent.click(await screen.findByRole('button', {name: '查看网关限制'}));
  expect(await screen.findByText(/创建时间时区未知/)).toBeInTheDocument();
});

it('解决冲突须明确选择并提交整组版本，409 保留选择和原因', async () => {
  const disputed = {...memory, status: 'disputed', effective_status: 'disputed', conflict_set_id: 'conflict-one'};
  const alternative = {...disputed, id: 2, revision: 4, key: '另一结论', content: '413 来自上游模型服务'};
  const fetcher = setup((path, options) => {
    if (path.endsWith('/memories/1')) return Response.json(disputed);
    if (path.endsWith('/memories/1/conflicts')) return Response.json({items: [disputed, alternative], next_cursor: null});
    if (path.endsWith('/memories/resolve') && options?.method === 'POST') return Response.json({message: '冲突集合版本已变化'}, {status: 409});
    return undefined;
  });
  render(<TaggedMemories token="offline" />);
  fireEvent.click(await screen.findByRole('button', {name: '查看网关限制'}));
  fireEvent.click(await screen.findByRole('button', {name: '查看冲突'}));
  const choice = await screen.findByRole('radio', {name: '采用另一结论'});
  const save = screen.getByRole('button', {name: '确认采用结论'});
  expect(save).toBeDisabled();
  fireEvent.click(choice);
  fireEvent.change(screen.getByLabelText('解决原因'), {target: {value: '已检查上游日志'}});
  fireEvent.click(save);
  expect(await screen.findByText('冲突集合版本已变化')).toBeInTheDocument();
  expect(choice).toBeChecked();
  expect(screen.getByLabelText('解决原因')).toHaveValue('已检查上游日志');
  const writes = fetcher.mock.calls.filter(([url]) => String(url).endsWith('/memories/resolve'));
  expect(writes).toHaveLength(1);
  expect(JSON.parse(String(writes[0][1]?.body))).toEqual({target_id: 2,
    items: [{id: 1, expected_revision: 3}, {id: 2, expected_revision: 4}], reason: '已检查上游日志'});
});

it('已遗忘记忆的普通详情不可读时仍能进入管理员版本审计', async () => {
  setup(path => {
    if (path.includes('/memories?')) return Response.json({items: [{...memory, status: 'deleted', effective_status: 'deleted'}], next_cursor: null});
    if (path.endsWith('/memories/1')) return Response.json({message: '记忆不存在或已删除'}, {status: 404});
    if (path.includes('/revisions')) return Response.json({items: [{id: 1, memory_id: 1, revision: 1,
      operation: 'create', snapshot: memory, reason: '原始记录审计', created_at: '2026-10-20T01:00:00Z'}], next_cursor: null});
    return undefined;
  });
  render(<TaggedMemories token="offline" />);
  fireEvent.click(await screen.findByRole('button', {name: '查看网关限制'}));
  await screen.findByText('记忆不存在或已删除');
  fireEvent.click(screen.getByRole('button', {name: '版本历史'}));
  expect(await screen.findByText('原始记录审计')).toBeInTheDocument();
});
