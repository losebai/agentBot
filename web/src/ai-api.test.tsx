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

it('查看供应商密钥按身份和版本读取独立管理接口', async () => {
  const fetcher = vi.fn(async (_url: string | URL | Request) => Response.json({ credential: 'saved-key' }));
  vi.stubGlobal('fetch', fetcher);
  expect(await new AiApi('offline').providerCredential(7, 3)).toEqual({ credential: 'saved-key' });
  expect(fetcher.mock.calls[0][0]).toBe('/api/v1/ai/providers/7/credential?expected_revision=3');
});
