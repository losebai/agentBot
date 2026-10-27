import { afterEach, beforeEach, vi } from 'vitest';
import type { MockInstance } from 'vitest';
import { ApiClient } from './api';

/** 其他页面测试明确假定插件已启动；生命周期展示由独立集成测试覆盖。 */
export function useRunningPluginFixture(ids = ['qqbot', 'bilibili']) {
  let request: MockInstance<ApiClient['plugins']>;
  beforeEach(() => {
    request = vi.spyOn(ApiClient.prototype, 'plugins').mockResolvedValue(ids.map(id => ({
      id, name: id, version: 'offline', description: '', enabled: true, saved_enabled: true,
      status: 'running', restart_required: false,
    })));
  });
  afterEach(() => request.mockRestore());
}
