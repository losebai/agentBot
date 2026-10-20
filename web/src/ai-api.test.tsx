import { expect, it, vi } from 'vitest';
import { AiApi } from './ai-api';

it('同步模型仅发送上游标识，不再制造别名或跳过别名冲突', async () => {
  const writes: unknown[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    if (String(url).includes('upstream-models')) return Response.json({ provider_id: 9, revision: 4, models: ['shared'] });
    writes.push(JSON.parse(String(init?.body)));
    return Response.json({ imported: [{ id: 1 }], skipped: [], conflicts: [] });
  }));
  await new AiApi('offline').syncModels(9, 4);
  expect(writes).toEqual([{ expected_revision: 4, models: [{ upstream_model: 'shared' }] }]);
});
