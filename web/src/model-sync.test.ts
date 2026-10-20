import { expect, it, vi } from 'vitest';
import { AiApi } from './ai-api';

it('自动同步全部模型超过单批上限，重复同步保留已有启用状态', async () => {
  const records = new Map<string, { alias: string | null; enabled: boolean }>([['existing', { alias: 'custom-name', enabled: true }]]);
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
      else { records.set(item.upstream_model, { alias: null, enabled: false }); imported.push(item); }
    }
    return Response.json({ provider_id: 7, revision: 3, imported, skipped, conflicts: [] });
  }));
  const api = new AiApi('offline');
  expect(await api.syncModels(7, 3)).toEqual({ total: 206, added: 205, conflicts: [] });
  expect(records.size).toBe(206);
  expect(records.get('model-204')).toEqual({ alias: null, enabled: false });
  expect(await api.syncModels(7, 3)).toEqual({ total: 206, added: 0, conflicts: [] });
  expect(records.get('existing')).toEqual({ alias: 'custom-name', enabled: true });
  expect(batches).toEqual([100, 100, 6, 100, 100, 6]);
});

it('跨供应商同步保留完整上游名称，不生成别名或截断合法长名称', async () => {
  const names = ['a'.repeat(199) + '1', 'a'.repeat(199) + '2'];
  const upstreams: string[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    if (String(url).includes('upstream-models')) return Response.json({ provider_id: 1, revision: 1, models: names });
    const body = JSON.parse(String(init.body));
    expect(body.models.map((item: { upstream_model: string }) => item.upstream_model)).toEqual(names);
    expect(body.models.every((item: object) => !('alias' in item))).toBe(true);
    upstreams.push(...body.models.map((item: { upstream_model: string }) => item.upstream_model));
    return Response.json({ imported: body.models, skipped: [], conflicts: [] });
  }));
  const api = new AiApi('offline');
  await api.syncModels(1, 1);
  await api.syncModels(2, 1);
  expect(upstreams).toEqual([...names, ...names]);
});

it('后续批次导入失败明确报错并停止，不把失败模型当作已导入', async () => {
  let batches = 0;
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    if (String(url).includes('upstream-models')) return Response.json({ revision: 1, models: Array.from({ length: 201 }, (_, i) => 'model-' + i) });
    batches += 1;
    if (batches === 2) return Response.json({ message: '导入失败' }, { status: 409 });
    return Response.json({ imported: JSON.parse(String(init.body)).models, skipped: [], conflicts: [] });
  }));
  await expect(new AiApi('offline').syncModels(1, 1)).rejects.toThrow('导入失败');
  expect(batches).toBe(2);
});

it('取消同步后不再提交后续批次', async () => {
  const controller = new AbortController();
  const fetcher = vi.fn(async (url, init) => {
    if (String(url).includes('upstream-models')) return Response.json({ revision: 2, models: Array.from({ length: 101 }, (_, i) => 'm' + i) });
    controller.abort();
    return Response.json({ imported: JSON.parse(String(init.body)).models, skipped: [], conflicts: [] });
  });
  vi.stubGlobal('fetch', fetcher);
  await expect(new AiApi('offline').syncModels(1, 2, controller.signal)).rejects.toThrow();
  expect(fetcher).toHaveBeenCalledTimes(2);
});
