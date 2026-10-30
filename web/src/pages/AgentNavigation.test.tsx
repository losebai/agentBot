import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { App } from '../App';

const agent = { id: 1, revision: 1, code: 'helper', name: '助手', description: '',
  enabled: true, model_id: 1, system_prompt: '原角色', personality_prompt: '',
  tools: [], skill_ids: [], mcp_ids: [], options: {} };

/** 导航使用真实页面与离线接口，演进数据只在进入对应二级页面时读取。 */
function setup(pending = false, empty = false, failAgents = false) {
  let evolutionSignal: AbortSignal | null | undefined;
  const fetcher = vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
    const path = String(input);
    if (path.endsWith('/auth')) return Response.json({ password_required: false });
    if (path.includes('/evolution/records?')) return Response.json({ items: [], next_cursor: null });
    if (path.includes('/evolution?')) {
      evolutionSignal = options?.signal;
      if (pending) return new Promise<Response>(() => {});
      return Response.json({ config: { enabled: true, revision: 0 }, items: [], next_cursor: null });
    }
    if (path.includes('/agents?')) return failAgents
      ? Response.json({ message: '角色目录暂不可用' }, { status: 503 })
      : Response.json({ items: empty ? [] : [agent], next_cursor: null });
    if (path.endsWith('/plugins') || path.endsWith('/bots')) return Response.json([]);
    if (path.includes('/ai/models?') || path.includes('/mcp-servers?')) return Response.json({ items: [], next_cursor: null });
    return Response.json({ service: 'agentBot', ready: true, bot_count: 0,
      database_revision: 1, applied_revision: 1, restart_required_fields: [] });
  });
  vi.stubGlobal('fetch', fetcher);
  return { fetcher, signal: () => evolutionSignal };
}

it('角色和人格包含自我演进二级菜单，切换页面保留角色草稿', async () => {
  setup();
  render(<App />);
  const nav = await screen.findByRole('navigation', { name: '主导航' });
  const submenu = within(nav).getByRole('group', { name: '角色和人格子菜单' });
  fireEvent.click(within(submenu).getByRole('button', { name: '角色配置' }));
  const role = await screen.findByLabelText('角色');
  fireEvent.change(role, { target: { value: '尚未保存的角色' } });
  expect(screen.queryByRole('region', { name: '自我演进管理' })).not.toBeInTheDocument();
  fireEvent.click(within(submenu).getByRole('button', { name: '自我演进' }));
  expect(await screen.findByRole('heading', { name: '自我演进' })).toBeVisible();
  expect(await screen.findByRole('checkbox', { name: '启用长期互动演化' })).toBeVisible();
  expect(role).not.toBeVisible();
  expect(within(submenu).getByRole('button', { name: '自我演进' })).toHaveAttribute('aria-current', 'page');
  fireEvent.click(within(submenu).getByRole('button', { name: '角色配置' }));
  expect(role).toBeVisible();
  expect(role).toHaveValue('尚未保存的角色');
  expect(screen.queryByRole('region', { name: '自我演进管理' })).not.toBeInTheDocument();
});

it('切换运行角色后返回角色页面刷新置顶顺序，同时保留已有编辑草稿', async () => {
  const server = setup();
  const first = { ...agent, id: 1, code: 'first', name: '原运行角色' };
  const second = { ...agent, id: 2, code: 'second', name: '新运行角色' };
  let switched = false;
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
    if (String(input).includes('/agents?')) return Response.json({
      items: switched ? [second, first] : [first, second], next_cursor: null,
    });
    return server.fetcher(input, options);
  }));
  render(<App />);
  const nav = await screen.findByRole('navigation', { name: '主导航' });
  fireEvent.click(within(nav).getByRole('button', { name: '角色配置' }));
  const role = await screen.findByLabelText('角色');
  fireEvent.change(role, { target: { value: '保留原角色草稿' } });
  fireEvent.click(within(nav).getByRole('button', { name: /运行概览$/ }));
  switched = true;
  fireEvent.click(within(nav).getByRole('button', { name: '角色配置' }));
  const switches = screen.getByRole('group', { name: '切换 Agent' });
  await waitFor(() => expect(within(switches).getAllByRole('button')[0])
    .toHaveAccessibleName('新运行角色'));
  expect(role).toHaveValue('保留原角色草稿');
  expect(within(switches).getByRole('button', { name: '原运行角色' }))
    .toHaveAttribute('aria-pressed', 'true');
});

it('离开自我演进子页面时取消正在读取的演进状态', async () => {
  const server = setup(true);
  render(<App />);
  const nav = await screen.findByRole('navigation', { name: '主导航' });
  fireEvent.click(within(nav).getByRole('button', { name: '自我演进' }));
  await waitFor(() => expect(server.signal()).toBeDefined());
  fireEvent.click(within(nav).getByRole('button', { name: /运行概览$/ }));
  expect(server.signal()?.aborted).toBe(true);
  expect(screen.queryByRole('heading', { name: '自我演进' })).not.toBeInTheDocument();
});

it('未创建 Agent 时仍能启停通用演化并查看独立记录列表', async () => {
  const server = setup(false, true);
  render(<App />);
  const nav = await screen.findByRole('navigation', { name: '主导航' });
  fireEvent.click(within(nav).getByRole('button', { name: '自我演进' }));
  expect(await screen.findByRole('checkbox', { name: '启用长期互动演化' })).toBeVisible();
  expect(screen.getByRole('button', { name: '未关联角色（通用）' })).toHaveAttribute('aria-pressed', 'true');
  expect(screen.getByRole('region', { name: '演化记录列表' })).toBeVisible();
  expect(server.fetcher.mock.calls.some(([url]) => String(url) === '/api/v1/evolution?cursor=0')).toBe(true);
});

it('角色目录读取失败仍可管理通用演化', async () => {
  setup(false, false, true);
  render(<App />);
  const nav = await screen.findByRole('navigation', { name: '主导航' });
  fireEvent.click(within(nav).getByRole('button', { name: '自我演进' }));
  await screen.findByText('角色目录暂不可用');
  expect(await screen.findByRole('checkbox', { name: '启用长期互动演化' })).toBeVisible();
  expect(screen.getByRole('button', { name: '未关联角色（通用）' })).toHaveAttribute('aria-pressed', 'true');
});

it('默认通用范围与可选角色范围使用各自状态，记录列表仍默认全部范围', async () => {
  const server = setup();
  render(<App />);
  const nav = await screen.findByRole('navigation', { name: '主导航' });
  fireEvent.click(within(nav).getByRole('button', { name: '自我演进' }));
  const selector = await screen.findByRole('group', { name: '选择演进 Agent' });
  expect(within(selector).getByRole('button', { name: '未关联角色（通用）' })).toHaveAttribute('aria-pressed', 'true');
  fireEvent.click(await within(selector).findByRole('button', { name: /助\s*手/ }));
  await waitFor(() => expect(server.fetcher.mock.calls.some(([url]) => String(url) === '/api/v1/evolution?cursor=0&agent_id=1')).toBe(true));
  fireEvent.click(within(selector).getByRole('button', { name: '未关联角色（通用）' }));
  expect(within(selector).getByRole('button', { name: '未关联角色（通用）' })).toHaveAttribute('aria-pressed', 'true');
  expect(screen.getByLabelText('记录范围')).toHaveValue('all');
});
