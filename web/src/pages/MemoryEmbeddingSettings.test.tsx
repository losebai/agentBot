import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { Settings } from './Settings';

/** 真实设置页使用模型目录；不允许把清空字符串变成静态回填或 null。 */
it('记忆向量模型从启用目录选择，可清空为未配置字符串', async () => {
  let value = 'embed-current';
  const fetcher = vi.fn(async (url: RequestInfo | URL, options?: RequestInit) => {
    if (String(url).includes('/ai/models?')) return Response.json({items: [
      {id: 1, revision: 1, alias: 'embed-current', upstream_model: 'embedding-one', provider_id: 1, enabled: true},
      {id: 2, revision: 1, alias: 'embed-next', upstream_model: 'embedding-two', provider_id: 1, enabled: true},
      {id: 3, revision: 1, alias: 'embed-disabled', upstream_model: 'embedding-three', provider_id: 1, enabled: false},
    ], next_cursor: null});
    if (options?.method === 'PATCH') value = JSON.parse(String(options.body)).values.chat_memory_embedding_model;
    return Response.json({database_revision: 1, applied_revision: 1, restart_required_fields: [], startup_revision: 'file-one', fields: [
      {name: 'chat_memory_embedding_model', description: '长期记忆向量模型', kind: 'string', value,
        nullable: false, secret_is_set: false, restart_required: false, scope: 'runtime', read_only: false},
    ]});
  });
  vi.stubGlobal('fetch', fetcher);
  render(<Settings token="offline" />);
  fireEvent.click(screen.getByRole('button', {name: 'LLM设置'}));
  const select = await screen.findByRole('combobox', {name: '长期记忆向量模型'});
  await waitFor(() => expect(select).toBeEnabled());
  expect(within(select).queryByRole('option', {name: 'embed-disabled'})).not.toBeInTheDocument();
  fireEvent.change(select, {target: {value: 'embed-next'}});
  fireEvent.click(screen.getByRole('button', {name: '保存修改'}));
  await waitFor(() => expect(value).toBe('embed-next'));
  await waitFor(() => expect(screen.getByRole('button', {name: '保存修改'})).toBeDisabled());
  expect(within(select).getByRole('option', {name: '未配置'})).toBeEnabled();
  fireEvent.change(select, {target: {value: ''}});
  fireEvent.click(screen.getByRole('button', {name: '保存修改'}));
  await waitFor(() => expect(value).toBe(''));
  const writes = fetcher.mock.calls.filter(([, options]) => options?.method === 'PATCH');
  expect(JSON.parse(String(writes.at(-1)?.[1]?.body))).toEqual({expected_revision: 1,
    values: {chat_memory_embedding_model: ''}});
});
