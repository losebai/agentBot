import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { TaggedMemories } from '../components/TaggedMemories';

/** 通过真实管理页面验证标题出现在筛选、新增及详情，HTTP 仅作为隔离边界。 */
it('长期记忆用用户名和群名显示作用域标题', async () => {
  const scope = 'a'.repeat(64);
  const item = {id: 1, scope: 'user', scope_id: scope, scope_title: '小明 (30001)',
    key: '语言', content: '偏好中文', tags: ['语言'], created_at: '2026-10-20T01:00:00'};
  vi.stubGlobal('fetch', vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.includes('/memory-scopes')) return Response.json([
      {scope_id: scope, scope: 'user', scope_title: item.scope_title, count: 1},
      {scope_id: 'b'.repeat(64), scope: 'group', scope_title: '读书群 (20001)', count: 2},
    ]);
    if (path.includes('/memory-tags')) return Response.json([{tag: '语言', count: 1}]);
    if (path.endsWith('/memories/1')) return Response.json(item);
    return Response.json({items: [item], next_cursor: null});
  }));
  render(<TaggedMemories token="offline" />);
  expect(await screen.findByRole('option', {name: '用户 · 小明 (30001)'})).toBeVisible();
  expect(screen.getByRole('option', {name: '群聊 · 读书群 (20001)'})).toBeVisible();
  fireEvent.click(screen.getByRole('button', {name: '查看语言'}));
  await waitFor(() => expect(screen.getByText('作用域：用户 · 小明 (30001)')).toBeVisible());
});
