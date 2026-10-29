import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { App } from './App';

/** 以具体按钮文本定位并验证真实可见性，避免对整页所有按钮重复计算样式。 */
function getVisibleButton(name: string | RegExp, root: HTMLElement = document.body) {
  const button = within(root).getByText(name, { selector: 'button, button > span' }).closest('button');
  if (!button) throw new Error('按钮文本没有对应的按钮');
  expect(button).toBeVisible();
  return button;
}

/** 从弹窗自身标题定位容器，后续表单和操作均限定在该弹窗内。 */
async function findVisibleDialog(title: string) {
  return waitFor(() => {
    const dialog = screen.getByText(title, { selector: '.ant-modal-title' }).closest<HTMLElement>('[role="dialog"]');
    if (!dialog) throw new Error('标题没有对应的弹窗');
    expect(dialog).toBeVisible();
    return dialog;
  });
}

/** 使用离线响应验证登录流程，避免连接真实后端或机器人。 */
function managementResponse(url: string) {
  if (url.endsWith('/plugins')) return Response.json([]);
  return url.endsWith('/bots')
    ? Response.json([{ id: '123456', adapter: 'OneBot V11' }])
    : Response.json({ service: 'agentBot', ready: true, bot_count: 1,
      database_revision: 4, applied_revision: 3, restart_required_fields: ['port'],
      ai_backend: 'python-ordinary' });
}

it.each(['侧边菜单', '插件中心'])('%s 可以进入真实表情包配置并保留未保存草稿', async entry => {
  const fetcher = vi.fn(async (url, _options?: RequestInit) => {
    const path = String(url);
    if (path.endsWith('/auth')) return Response.json({ password_required: false });
    if (path.endsWith('/plugins')) return Response.json([{
      id: 'stickers', name: '表情包', version: 'offline', description: '表情图库',
      enabled: true, saved_enabled: true, status: 'running', restart_required: false,
    }]);
    if (path.endsWith('/settings')) return Response.json({
      database_revision: 1, applied_revision: 1, restart_required_fields: [], fields: [{
        name: 'chat_sticker_vision_prompt', description: '表情包识图要求', kind: 'string',
        value: '', nullable: false, secret_is_set: false, restart_required: false,
      }],
    });
    if (path.includes('/stickers?')) return Response.json({ items: [], total: 0, next_offset: null });
    return managementResponse(path);
  });
  vi.stubGlobal('fetch', fetcher);
  render(<App />);
  const navigation = await screen.findByLabelText('主导航');
  if (entry === '插件中心') {
    fireEvent.click(await within(navigation).findByText('插件中心', { selector: 'button' }));
    fireEvent.click(await screen.findByLabelText('配置 表情包'));
  } else {
    fireEvent.click(await within(navigation).findByText('表情包配置', { selector: 'button' }));
  }
  expect(await screen.findByText('表情包配置', { selector: 'h1' })).toBeVisible();
  expect(screen.getByText('表情包配置', { selector: '.topbar-title' })).toBeVisible();
  expect(await screen.findByLabelText('表情包图库')).toBeVisible();
  expect(getVisibleButton('添加表情包')).toBeVisible();
  fireEvent.click(screen.getByRole('tab', { name: '配置' }));
  fireEvent.change(await screen.findByLabelText('表情包识图要求'), {
    target: { value: '说明适合表达的语气' },
  });
  fireEvent.click(getVisibleButton(/运行概览$/, navigation));
  fireEvent.click(await within(navigation).findByText('表情包配置', { selector: 'button' }));
  await waitFor(() => expect(screen.getByLabelText('表情包识图要求')).toBeVisible());
  expect(screen.getByLabelText('表情包识图要求')).toHaveValue('说明适合表达的语气');
  expect(fetcher.mock.calls.every(([, options]) => options?.method !== 'PATCH')).toBe(true);
});

it('主导航可进入标签与知识库，离开后不保留详情页', async () => {
  vi.stubGlobal('fetch', vi.fn(async url => {
    const path = String(url);
    if (path.endsWith('/auth')) return Response.json({password_required: false});
    if (path.includes('knowledge-tags')) return Response.json([]);
    if (path.includes('/knowledge/count')) return Response.json({total: 0});
    if (path.includes('/knowledge?')) return Response.json({items: [], next_cursor: null});
    return managementResponse(path);
  }));
  render(<App />);
  const nav = await screen.findByRole('navigation');
  fireEvent.click(within(nav).getByRole('button', {name: /标签与知识库$/}));
  expect(await screen.findByRole('button', {name: '新增知识'})).toBeInTheDocument();
  fireEvent.click(within(nav).getByRole('button', {name: /运行概览$/}));
  expect(screen.queryByRole('button', {name: '新增知识'})).not.toBeInTheDocument();
});

it('机器人配置提供与 LLM 同级的记忆入口', async () => {
  vi.stubGlobal('fetch', vi.fn(async url => {
    const path = String(url);
    if (path.endsWith('/auth')) return Response.json({ password_required: false });
    if (path.endsWith('/settings')) return Response.json({
      database_revision: 1, applied_revision: 1, restart_required_fields: [], fields: [
        { name: 'chat_memory_enabled', description: '会话记忆', kind: 'boolean', value: true,
          nullable: false, secret_is_set: false, restart_required: false },
      ],
    });
    return managementResponse(path);
  }));
  render(<App />);
  const navigation = await screen.findByRole('navigation', { name: '主导航' });
  fireEvent.click(within(navigation).getByRole('button', { name: /机器人配置$/ }));
  const categories = await screen.findByRole('navigation', { name: '机器人配置分类' });
  expect(within(categories).getAllByRole('button').map(button => button.textContent)).toEqual([
    '基础设置', 'LLM设置', '记忆设置', '自动回复设置',
  ]);
  fireEvent.click(within(categories).getByRole('button', { name: '记忆设置' }));
  expect(await screen.findByLabelText('会话记忆')).toBeVisible();
});

it('密码校验失败时保持登录页面且不持久化密码', async () => {
  vi.stubGlobal('fetch', vi.fn(async (url) => String(url).endsWith('/auth')
    ? Response.json({ password_required: true })
    : Response.json({ code: 'UNAUTHORIZED', message: '管理密码无效', request_id: 'offline' }, { status: 401 })));
  const persist = vi.spyOn(Storage.prototype, 'setItem');
  render(<App />);
  fireEvent.change(await screen.findByLabelText('管理员密码'), { target: { value: 'Wrong!123' } });
  fireEvent.click(screen.getByRole('button', { name: /登\s*录/ }));
  expect(await screen.findByText('管理密码无效')).toBeInTheDocument();
  expect(screen.queryByRole('navigation')).not.toBeInTheDocument();
  expect(persist).not.toHaveBeenCalled();
  persist.mockRestore();
});

it('自定义密码登录后显示真实状态，退出清空密码', async () => {
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    if (String(url).endsWith('/auth')) return Response.json({ password_required: true });
    if (new Headers(init?.headers).get('Authorization') !== 'Bearer Easy!123') {
      return Response.json({ message: '管理密码无效' }, { status: 401 });
    }
    return managementResponse(String(url));
  }));
  render(<App />);
  fireEvent.change(await screen.findByLabelText('管理员密码'), { target: { value: 'Easy!123' } });
  fireEvent.click(screen.getByRole('button', { name: /登\s*录/ }));
  expect(await screen.findByRole('navigation')).toBeInTheDocument();
  expect(await screen.findByText('123456')).toBeInTheDocument();
  expect(screen.getByText(/以下设置需要重启：port/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '退出管理' }));
  expect(screen.getByLabelText('管理员密码')).toHaveValue('');
  expect(screen.queryByRole('navigation')).not.toBeInTheDocument();
});

it('未配置密码时直接进入系统，管理请求不发送空认证头', async () => {
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    if (new Headers(init?.headers).has('Authorization')) {
      return Response.json({ message: '不应发送空认证头' }, { status: 400 });
    }
    return String(url).endsWith('/auth')
      ? Response.json({ password_required: false })
      : managementResponse(String(url));
  }));
  render(<App />);
  expect(await screen.findByRole('navigation')).toBeInTheDocument();
  expect(await screen.findByText('123456')).toBeInTheDocument();
  expect(screen.queryByLabelText('管理员密码')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '退出管理' })).not.toBeInTheDocument();
});

it('查询登录模式失败时显示错误，不能误进入系统', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => Response.json({ message: '服务暂时无法完成请求' }, { status: 503 })));
  render(<App />);
  expect(await screen.findByText('服务暂时无法完成请求')).toBeInTheDocument();
  expect(screen.queryByRole('navigation')).not.toBeInTheDocument();
  expect(screen.queryByLabelText('管理员密码')).not.toBeInTheDocument();
});

it('角色和人格直接展示配置，远程服务从一级导航独立创建', async () => {
  const servers: Record<string, unknown>[] = [];
  const agent = { id: 1, revision: 1, code: 'helper', name: '助手', description: '',
    enabled: true, model_id: 1, vision_model_id: null, fallback_model_id: null,
    tools: [], skill_ids: [], mcp_ids: [], system_prompt: '', max_steps: 3, options: {} };
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    const path = new URL(String(url), 'http://offline.test').pathname;
    if (path.endsWith('/auth')) return Response.json({ password_required: false });
    if (path.endsWith('/mcp-servers')) {
      if (init?.method === 'POST') {
        const server = { ...JSON.parse(String(init.body)), id: 7, revision: 1 };
        servers.push(server);
        return Response.json(server, { status: 201 });
      }
      return Response.json({ items: servers, next_cursor: null });
    }
    if (path.endsWith('/agents')) return Response.json({ items: [agent], next_cursor: null });
    if (path.endsWith('/models') || path.endsWith('/skills')) {
      return Response.json({ items: [], next_cursor: null });
    }
    return managementResponse(String(url));
  }));
  render(<App />);
  const navigation = await screen.findByRole('navigation', { name: '主导航' });
  fireEvent.click(getVisibleButton(/角色和人格$/, navigation));
  expect(await screen.findByRole('heading', { name: '角色和人格' })).toBeVisible();
  expect(await screen.findByRole('group', { name: '切换 Agent' })).toBeVisible();
  expect(screen.queryByRole('tablist')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '新增 Skill' })).not.toBeInTheDocument();
  fireEvent.click(getVisibleButton(/远程服务$/, navigation));
  expect(await screen.findByRole('heading', { name: '远程服务' })).toBeVisible();
  expect(screen.queryByRole('group', { name: '切换 Agent' })).not.toBeInTheDocument();
  await waitFor(() => expect(getVisibleButton('新增 远程服务')).toBeEnabled());
  fireEvent.click(getVisibleButton('新增 远程服务'));
  const dialog = await findVisibleDialog('新增 远程服务');
  fireEvent.change(within(dialog).getByLabelText('名称'), { target: { value: '资料服务' } });
  fireEvent.change(within(dialog).getByLabelText('服务代码'), { target: { value: 'library' } });
  fireEvent.change(within(dialog).getByLabelText('服务地址'), { target: { value: 'http://localhost:9000/mcp' } });
  fireEvent.click(getVisibleButton('保存 远程服务', dialog));
  await waitFor(() => expect(dialog).not.toBeInTheDocument());
  expect(await screen.findByText('资料服务')).toBeVisible();
  fireEvent.click(getVisibleButton(/角色和人格$/, navigation));
  expect(await screen.findByRole('group', { name: '切换 Agent' })).toBeVisible();
  expect(screen.getByRole('button', { name: /远程服务$/ })).toBeVisible();
}, 20000);
