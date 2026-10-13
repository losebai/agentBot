import { expect, it, vi } from 'vitest';
import { AiApi } from './ai-api';

it('自动同步全部模型超过单批上限，重复同步保留已有启用状态', async () => {
  const records = new Map<string, { alias: string; enabled: boolean }>([['existing', { alias: 'custom-name', enabled: true }]]);
  const names = ['existing', ...Array.from({ length: 205 }, (_, i) => 'model-' + i)];
  const batches: number[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    if (String(url).includes('upstream-models')) return Response.json({ provider_id: 7, revision: 3, models: names });
    const body = JSON.parse(String(init.body));
    expect(body.expected_revision).toBe(3);
    batches.push(body.models.length);
    if (body.models.length > 100) return Response.json({ message: '批次超限' }, { status: 422 });
    const imported: unknown[] = [], skipped: string[] = [];
    for (const item of body.models) {
      if (records.has(item.upstream_model)) skipped.push(item.upstream_model);
      else { records.set(item.upstream_model, { alias: item.alias, enabled: false }); imported.push(item); }
    }
    return Response.json({ provider_id: 7, revision: 3, imported, skipped });
  }));
  const api = new AiApi('offline');
  expect(await api.syncModels(7, 3)).toEqual({ total: 206, added: 205 });
  expect(records.size).toBe(206);
  expect(records.get('model-204')).toEqual({ alias: '7/model-204', enabled: false });
  expect(await api.syncModels(7, 3)).toEqual({ total: 206, added: 0 });
  expect(records.get('existing')).toEqual({ alias: 'custom-name', enabled: true });
  expect(batches).toEqual([100, 100, 6, 100, 100, 6]);
});

it('局域网 HTTP 页面也能保留完整长 ID 并生成不超限且不混用的别名', async () => {
  // 普通 HTTP 局域网页面不提供 SubtleCrypto，但仍应能同步合法长模型 ID。
  vi.stubGlobal('crypto', {});
  const names = ['a'.repeat(199) + '1', 'a'.repeat(199) + '2'];
  const aliases: string[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    if (String(url).includes('upstream-models')) return Response.json({ provider_id: 1, revision: 1, models: names });
    const body = JSON.parse(String(init.body));
    expect(body.models.map((item: { upstream_model: string }) => item.upstream_model)).toEqual(names);
    aliases.push(...body.models.map((item: { alias: string }) => item.alias));
    return Response.json({ imported: body.models, skipped: [] });
  }));
  const api = new AiApi('offline');
  await api.syncModels(1, 1);
  await api.syncModels(2, 1);
  expect(aliases.every(alias => alias.length <= 200)).toBe(true);
  expect(new Set(aliases).size).toBe(4);
});

it('取消同步后不再提交后续批次', async () => {
  const controller = new AbortController();
  const fetcher = vi.fn(async (url, init) => {
    if (String(url).includes('upstream-models')) return Response.json({ revision: 2, models: Array.from({ length: 101 }, (_, i) => 'm' + i) });
    controller.abort();
    return Response.json({ imported: JSON.parse(String(init.body)).models, skipped: [] });
  });
  vi.stubGlobal('fetch', fetcher);
  await expect(new AiApi('offline').syncModels(1, 2, controller.signal)).rejects.toThrow();
  expect(fetcher).toHaveBeenCalledTimes(2);
});
