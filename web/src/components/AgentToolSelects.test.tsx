import { useRunningPluginFixture } from '../test-plugins';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useState } from 'react';
import { expect, it, vi } from 'vitest';
import { AgentToolSelects } from './AgentToolSelects';

useRunningPluginFixture();

/** 用受控草稿检查实际选择结果，不访问真实 MCP。 */
function Editor({ initialTools = [] as string[], initialMcps = [] as number[] }) {
  const [tools, setTools] = useState(initialTools), [mcps, setMcps] = useState(initialMcps);
  return <><AgentToolSelects token="offline" tools={tools} mcpIds={mcps} onToolsChange={setTools} onMcpsChange={setMcps} />
    <output aria-label="工具草稿">{JSON.stringify(tools)}</output><output aria-label="MCP 草稿">{JSON.stringify(mcps)}</output></>;
}

it.each([
  ['知识库检索', 'knowledge_search'], ['素材读取', 'material_get'],
  ['B站我的视频', 'bilibili_get_my_videos'],
])('%s 不会自动勾选，用户明确选择后写入工具草稿', async (label, name) => {
  vi.stubGlobal('fetch', vi.fn(async () => Response.json({items: [], next_cursor: null})));
  render(<Editor />);
  expect(screen.getByLabelText('工具草稿')).toHaveTextContent('[]');
  fireEvent.mouseDown(screen.getByRole('combobox', {name: '允许工具'}));
  fireEvent.click(await screen.findByText(label + ' · ' + name));
  expect(screen.getByLabelText('工具草稿')).toHaveTextContent(JSON.stringify([name]));
});

it('MCP 下拉覆盖所有分页，关联后自动使用工具而无需逐个勾选', async () => {
  vi.stubGlobal('fetch', vi.fn(async url => Response.json(String(url).includes('cursor=next')
    ? { items: [{ id: 2, name: '文档服务', code: 'docs', category: 'general', enabled: true, allowed_tools: ['find_docs'] }], next_cursor: null }
    : { items: [{ id: 1, name: '搜索服务', code: 'search', category: 'search', enabled: true, allowed_tools: ['search'] }], next_cursor: 'next' })));
  render(<Editor />);
  await waitFor(() => expect(screen.getByRole('combobox', { name: '关联 MCP（最多 3 个）' })).toBeEnabled());
  fireEvent.mouseDown(screen.getByRole('combobox', { name: '关联 MCP（最多 3 个）' }));
  fireEvent.click(await screen.findByText('文档服务 · docs'));
  expect(screen.getByLabelText('MCP 草稿')).toHaveTextContent('[2]');
  fireEvent.mouseDown(screen.getByRole('combobox', { name: '关联 MCP（最多 3 个）' }));
  await waitFor(() => expect(screen.getByText('搜索服务 · search')).toBeVisible());
  fireEvent.click(screen.getByText('搜索服务 · search'));
  expect(screen.getByLabelText('MCP 草稿')).toHaveTextContent('[2,1]');
  expect(screen.getByText(/已关联 MCP 的工具自动可用/)).toBeVisible();
  fireEvent.mouseDown(screen.getByRole('combobox', { name: '允许工具' }));
  fireEvent.change(screen.getByRole('combobox', { name: '允许工具' }), { target: { value: 'find_docs' } });
  expect(screen.queryByText('find_docs · 文档服务')).not.toBeInTheDocument();
  expect(screen.getByLabelText('工具草稿')).toHaveTextContent('[]');
});

it('已选三个 MCP 时不能添加第四个，未知工具和关联保留', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => Response.json({ items: [
    { id: 4, name: '第四个服务', code: 'fourth', category: 'general', enabled: true, allowed_tools: ['*'] },
  ], next_cursor: null })));
  render(<Editor initialTools={['legacy_tool']} initialMcps={[1, 2, 3]} />);
  await waitFor(() => expect(screen.getByRole('combobox', { name: '关联 MCP（最多 3 个）' })).toBeEnabled());
  expect(screen.getByText('legacy_tool（原有配置）')).toBeVisible();
  expect(screen.getByText('MCP #1（未在当前目录）')).toBeVisible();
  fireEvent.mouseDown(screen.getByRole('combobox', { name: '关联 MCP（最多 3 个）' }));
  fireEvent.click(await screen.findByText('第四个服务 · fourth'));
  expect(screen.getByLabelText('MCP 草稿')).toHaveTextContent('[1,2,3]');
  expect(screen.getByLabelText('工具草稿')).toHaveTextContent('["legacy_tool"]');
});

it('加载失败可刷新恢复，通配符不作为工具选项，已有草稿不丢失', async () => {
  let failed = true;
  vi.stubGlobal('fetch', vi.fn(async () => failed ? Response.json({ message: '目录暂时不可用' }, { status: 503 })
    : Response.json({ items: [{ id: 9, name: '本地工具', code: 'local', category: 'general', enabled: true, allowed_tools: ['*'] }], next_cursor: null })));
  render(<Editor initialTools={['memory']} initialMcps={[9]} />);
  expect(await screen.findByText(/目录暂时不可用/)).toBeVisible();
  expect(screen.getByLabelText('工具草稿')).toHaveTextContent('["memory"]');
  failed = false;
  fireEvent.click(screen.getByRole('button', { name: '刷新 MCP 列表' }));
  expect(await screen.findByText('本地工具 · local')).toBeVisible();
  expect(screen.getByText(/白名单为 \* 时自动使用服务返回的全部工具/)).toBeVisible();
  fireEvent.mouseDown(screen.getByRole('combobox', { name: '允许工具' }));
  expect(screen.queryByText('*')).not.toBeInTheDocument();
  await waitFor(() => expect(screen.getByText('网页读取 · read_url')).toBeVisible());
});
