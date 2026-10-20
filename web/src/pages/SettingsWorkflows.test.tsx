import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { Settings } from './Settings';

/** 以具体按钮文本定位并验证真实可见性，避免对整页所有按钮重复计算样式。 */
function getVisibleButton(name: string | RegExp, root: HTMLElement = document.body) {
  const button = within(root).getByText(name, { selector: 'button, button > span' }).closest('button');
  if (!button) throw new Error('按钮文本没有对应的按钮');
  expect(button).toBeVisible();
  return button;
}

/** 页签具有直接可读名称，保留可见性校验后再执行切换。 */
function getVisibleTab(name: string, root: HTMLElement = document.body) {
  const tab = within(root).getByText(name, { selector: '[role="tab"]' });
  expect(tab).toBeVisible();
  return tab;
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


const settings = {
  database_revision: 1, applied_revision: 1, restart_required_fields: [],
  fields: [
    { name: 'chat_llm_temperature', description: '温度', kind: 'number', value: 0.7,
      nullable: true, secret_is_set: false, restart_required: false, scope: 'runtime', read_only: false },
    { name: 'chat_local_workflow_base_url', description: 'Comfy 服务地址', kind: 'string',
      value: 'http://127.0.0.1:8188', nullable: false, secret_is_set: false,
      restart_required: false, scope: 'runtime', read_only: false },
    { name: 'chat_local_workflow_default_key', description: '默认工作流', kind: 'string',
      value: 'sunrise', nullable: false, secret_is_set: false,
      restart_required: false, scope: 'runtime', read_only: false },
  ],
};
const workflow = {
  workflow_key: 'sunrise', display_name: '晨光画图', source_file: 'private/workflows/sunrise.json',
  source_format: 'api', content_sha256: 'content-hash', revision: 'revision-one', enabled: true,
  executable: true, description: '生成自然风景', additional_positive_prompt: '',
  additional_negative_prompt: '', workflow_type: 'text-to-image', positive_node_id: '1',
  negative_node_id: '2', output_node_id: '3', seed_node_id: '4', input_image_node_id: null,
  positive_input_name: 'text', negative_input_name: 'text',
};
const draftWorkflow = { ...workflow, workflow_key: 'draft', display_name: '未适配草稿',
  enabled: false, executable: false, source_format: 'ui', revision: 'draft-revision' };
const catalog = { items: [draftWorkflow, workflow], default_key: 'sunrise',
  settings_revision: 1, directory_configured: true, next_cursor: null };
const detail = { ...workflow, ui_json: '{"nodes":[]}', api_json: '{"1":{"inputs":{"text":"sun"}}}' };

/** 进入按需加载的工作流子页，测试真实设置页面的状态保留边界。 */
async function openWorkflows() {
  fireEvent.click(getVisibleButton('Comfy'));
  await screen.findByLabelText('Comfy 服务地址');
  fireEvent.click(getVisibleTab('工作流管理'));
  await screen.findByText('晨光画图');
}

describe('Comfy 工作流管理', () => {
  afterEach(() => vi.restoreAllMocks());

  it('只在工作流子页加载目录，筛选名称并阻止未适配工作流启用', async () => {
    const requests: string[] = [];
    vi.stubGlobal('fetch', vi.fn(async url => {
      requests.push(String(url));
      return Response.json(String(url).endsWith('/settings') ? settings : catalog);
    }));
    render(<Settings token="offline-token" />);
    fireEvent.click(getVisibleButton('Comfy'));
    expect(await screen.findByLabelText('Comfy 服务地址')).toBeInTheDocument();
    expect(requests.filter(path => path.endsWith('/workflows'))).toHaveLength(0);
    fireEvent.click(getVisibleTab('工作流管理'));
    expect(await screen.findByText('晨光画图')).toBeInTheDocument();
    expect(screen.getByLabelText('启用未适配草稿')).toBeDisabled();
    expect(screen.getByLabelText('将未适配草稿设为默认')).toBeDisabled();
    expect(screen.queryByText('private/workflows/sunrise.json')).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('筛选工作流'), { target: { value: '晨光' } });
    expect(screen.queryByText('未适配草稿')).not.toBeInTheDocument();
    expect(screen.getByText('晨光画图')).toBeInTheDocument();
  });

  it('加载更多工作流追加下一页并去重，筛选范围明确为已加载项', async () => {
    const requests: string[] = [];
    const later = { ...workflow, workflow_key: 'twilight', display_name: '晚霞画图' };
    vi.stubGlobal('fetch', vi.fn(async url => {
      const path = String(url); requests.push(path);
      return Response.json(path.endsWith('/settings') ? settings
        : path.includes('?after=') ? { ...catalog, items: [workflow, later] }
          : { ...catalog, next_cursor: 'sunrise' });
    }));
    render(<Settings token="offline-token" />);
    await openWorkflows();
    expect(screen.getByText('已加载 2 个工作流')).toBeInTheDocument();
    expect(screen.getByText('筛选已加载工作流')).toBeInTheDocument();
    fireEvent.click(getVisibleButton('加载更多'));
    expect(await screen.findByText('晚霞画图')).toBeInTheDocument();
    expect(screen.getAllByText('晨光画图')).toHaveLength(1);
    expect(requests).toContain('/api/v1/workflows?after=sunrise');
    expect(screen.getByText('已加载 3 个工作流')).toBeInTheDocument();
    expect(getVisibleButton('加载更多')).toBeDisabled();
    fireEvent.change(screen.getByLabelText('筛选工作流'), { target: { value: '晚霞' } });
    expect(screen.queryByText('晨光画图')).not.toBeInTheDocument();
    expect(screen.getByText('晚霞画图')).toBeInTheDocument();
  });

  it('导入 JSON 文件内容后刷新目录，目录同步沿用明确操作', async () => {
    const posts: unknown[] = [];
    let catalogLoads = 0;
    vi.stubGlobal('fetch', vi.fn(async (url, init) => {
      if (String(url).endsWith('/settings')) return Response.json(settings);
      if (init?.method === 'POST') {
        posts.push({ path: String(url), body: init.body ? JSON.parse(init.body) : undefined });
        return Response.json({ imported: 1 });
      }
      // 设置基础页还会读取队列状态，只统计工作流目录请求。
      if (String(url).endsWith('/workflows')) catalogLoads += 1;
      return Response.json(catalog);
    }));
    render(<Settings token="offline-token" />);
    await openWorkflows();
    const content = '{"nodes":[]}';
    fireEvent.change(screen.getByLabelText('导入 JSON 文件'), { target: { files: [new File([content], 'scene.json', { type: 'application/json' })] } });
    await waitFor(() => expect(posts).toContainEqual({ path: '/api/v1/workflows/import',
      body: { filename: 'scene.json', content } }));
    await waitFor(() => expect(catalogLoads).toBe(2));
    expect(await screen.findByText(/已导入 1 个工作流/)).toBeInTheDocument();
    fireEvent.click(getVisibleButton('同步配置目录'));
    await waitFor(() => expect(posts).toContainEqual({ path: '/api/v1/workflows/sync', body: undefined }));
    await waitFor(() => expect(catalogLoads).toBe(3));
  });

  it('超过 1 MiB 或无效 JSON 的文件在上传前拒绝，未配置目录不能同步', async () => {
    const posts: string[] = [];
    vi.stubGlobal('fetch', vi.fn(async (url, init) => {
      if (init?.method === 'POST') posts.push(String(url));
      return Response.json(String(url).endsWith('/settings') ? settings : { ...catalog, directory_configured: false });
    }));
    render(<Settings token="offline-token" />);
    await openWorkflows();
    expect(getVisibleButton('同步配置目录')).toBeDisabled();
    const input = screen.getByLabelText('导入 JSON 文件');
    fireEvent.change(input, { target: { files: [new File(['x'.repeat(1_048_577)], 'huge.json')] } });
    expect(await screen.findByText(/不能超过 1 MiB/)).toBeInTheDocument();
    fireEvent.change(input, { target: { files: [new File(['not-json'], 'broken.json')] } });
    expect(await screen.findByText(/文件内容不是有效 JSON/)).toBeInTheDocument();
    expect(posts).toEqual([]);
  });

  it('metadata 冲突后读取最新版本，仅保留用户修改并用新版本提交', async () => {
    const patches: unknown[] = [];
    let loads = 0;
    const latest = { ...detail, revision: 'revision-two', description: '他人更新的说明',
      additional_positive_prompt: '他人正向提示词', additional_negative_prompt: '他人负向提示词' };
    vi.stubGlobal('fetch', vi.fn(async (url, init) => {
      if (String(url).endsWith('/settings')) return Response.json(settings);
      if (init?.method === 'PATCH') {
        const patch = JSON.parse(init.body);
        patches.push(patch);
        // 模拟真实乐观锁：旧 revision 每次提交都冲突，只有显式重载后的新版本可保存。
        return patch.expected_revision !== 'revision-two'
          ? Response.json({ code: 'WORKFLOW_CONFLICT', message: '工作流已被修改，请重新加载' }, { status: 409 })
          : Response.json({ ...latest, ...patch.values, revision: 'revision-three' });
      }
      if (String(url).endsWith('/workflows/sunrise')) return Response.json(latest);
      // 其他设置面板请求不属于工作流刷新次数。
      if (String(url).endsWith('/workflows')) loads += 1;
      return Response.json(catalog);
    }));
    render(<Settings token="offline-token" />);
    await openWorkflows();
    fireEvent.click(screen.getByLabelText('编辑晨光画图'));
    const dialog = await findVisibleDialog('编辑工作流');
    fireEvent.change(within(dialog).getByLabelText('显示名称'), { target: { value: '傍晚画图' } });
    fireEvent.change(within(dialog).getByLabelText('附加正向提示词'), { target: { value: '用户的正向提示词' } });
    fireEvent.click(getVisibleButton('保存工作流', dialog));
    expect(await within(dialog).findByText('工作流已被修改，请重新加载')).toBeInTheDocument();
    expect(within(dialog).getByLabelText('显示名称')).toHaveValue('傍晚画图');
    expect(patches[0]).toEqual({ expected_revision: 'revision-one', values: {
      display_name: '傍晚画图', additional_positive_prompt: '用户的正向提示词',
    } });
    fireEvent.click(getVisibleButton('读取最新版本并保留修改', dialog));
    await waitFor(() => expect(within(dialog).getByLabelText('说明')).toHaveValue('他人更新的说明'));
    expect(within(dialog).getByLabelText('显示名称')).toHaveValue('傍晚画图');
    expect(within(dialog).getByLabelText('附加正向提示词')).toHaveValue('用户的正向提示词');
    expect(within(dialog).getByLabelText('附加负向提示词')).toHaveValue('他人负向提示词');
    expect(within(dialog).getByText(/请复核后再保存/)).toBeInTheDocument();
    fireEvent.click(getVisibleButton('保存工作流', dialog));
    await waitFor(() => expect(dialog).not.toBeInTheDocument());
    expect(patches[1]).toEqual({ expected_revision: 'revision-two', values: {
      display_name: '傍晚画图', additional_positive_prompt: '用户的正向提示词',
    } });
    await waitFor(() => expect(loads).toBe(2));
  });

  it('读取最新工作流失败保留草稿与版本，读取期间不能关闭或执行其他写操作', async () => {
    const patches: unknown[] = [];
    let rejectRead: ((response: Response) => void) | undefined;
    vi.stubGlobal('fetch', vi.fn(async (url, init) => {
      if (String(url).endsWith('/settings')) return Response.json(settings);
      if (init?.method === 'PATCH') {
        patches.push(JSON.parse(init.body));
        return Response.json({ code: 'WORKFLOW_CONFLICT', message: '工作流已被修改，请重新加载' }, { status: 409 });
      }
      if (String(url).endsWith('/workflows/sunrise')) {
        return new Promise<Response>(resolve => { rejectRead = resolve; });
      }
      return Response.json(catalog);
    }));
    render(<Settings token="offline-token" />);
    await openWorkflows();
    fireEvent.click(screen.getByLabelText('编辑晨光画图'));
    const dialog = await findVisibleDialog('编辑工作流');
    fireEvent.change(within(dialog).getByLabelText('显示名称'), { target: { value: '保留的名称' } });
    fireEvent.click(getVisibleButton('保存工作流', dialog));
    await within(dialog).findByText('工作流已被修改，请重新加载');
    fireEvent.click(getVisibleButton('读取最新版本并保留修改', dialog));
    await waitFor(() => expect(rejectRead).toBeDefined());
    expect(within(dialog).getByLabelText('取消')).toBeDisabled();
    expect(within(dialog).getByLabelText('显示名称')).toBeDisabled();
    expect(getVisibleButton(/保存工作流/, dialog)).toBeDisabled();
    expect(within(dialog).queryByRole('button', { name: 'Close' })).not.toBeInTheDocument();
    expect(screen.getByText('同步配置目录').closest('button')).toBeDisabled();
    rejectRead!(Response.json({ code: 'WORKFLOW_UNAVAILABLE', message: '无法读取最新工作流' }, { status: 503 }));
    expect(await within(dialog).findByText('无法读取最新工作流')).toBeInTheDocument();
    expect(within(dialog).getByLabelText('显示名称')).toHaveValue('保留的名称');
    fireEvent.click(getVisibleButton('保存工作流', dialog));
    await waitFor(() => expect(patches).toHaveLength(2));
    expect(patches[1]).toEqual({ expected_revision: 'revision-one', values: { display_name: '保留的名称' } });
  });

  it('停用工作流使用自身版本，后端失败保持原状态并显示错误', async () => {
    let patch: unknown;
    vi.stubGlobal('fetch', vi.fn(async (url, init) => {
      if (String(url).endsWith('/settings')) return Response.json(settings);
      if (init?.method === 'PATCH') {
        patch = JSON.parse(init.body);
        return Response.json({ code: 'WORKFLOW_INVALID', message: '工作流不能停用' }, { status: 422 });
      }
      return Response.json(catalog);
    }));
    render(<Settings token="offline-token" />);
    await openWorkflows();
    fireEvent.click(screen.getByLabelText('启用晨光画图'));
    expect(await screen.findByText('工作流不能停用')).toBeInTheDocument();
    expect(patch).toEqual({ expected_revision: 'revision-one', values: { enabled: false } });
    expect(screen.getByLabelText('启用晨光画图')).toBeChecked();
  });

  it('选择默认工作流刷新配置版本，保留其他分类草稿并移除旧默认草稿', async () => {
    const second = { ...workflow, workflow_key: 'evening', display_name: '傍晚画图' };
    let selected = false;
    let defaultBody: unknown;
    let settingsPatch: unknown;
    vi.stubGlobal('fetch', vi.fn(async (url, init) => {
      const path = String(url);
      if (path.endsWith('/settings')) {
        if (init?.method === 'PATCH') settingsPatch = JSON.parse(init.body);
        return Response.json(selected ? { ...settings, database_revision: 2, applied_revision: 2,
          fields: settings.fields.map(field => field.name === 'chat_local_workflow_default_key'
            ? { ...field, value: 'evening' } : field) } : settings);
      }
      if (path.endsWith('/default')) {
        defaultBody = JSON.parse(init.body); selected = true;
      }
      return Response.json({ ...catalog, items: [...catalog.items, second],
        default_key: selected ? 'evening' : 'sunrise', settings_revision: selected ? 2 : 1 });
    }));
    render(<Settings token="offline-token" />);
    fireEvent.click(getVisibleButton('LLM设置'));
    fireEvent.change(await screen.findByLabelText('温度'), { target: { value: '0.4' } });
    fireEvent.click(getVisibleButton('Comfy'));
    fireEvent.change(screen.getByLabelText('默认工作流'), { target: { value: 'unsubmitted' } });
    fireEvent.click(getVisibleTab('工作流管理'));
    fireEvent.click(await screen.findByLabelText('将傍晚画图设为默认'));
    expect(await screen.findByText('数据库版本 2 · 运行时版本 2')).toBeInTheDocument();
    expect(defaultBody).toEqual({ expected_revision: 1 });
    fireEvent.click(getVisibleTab('连接设置'));
    expect(screen.getByLabelText('默认工作流')).toHaveValue('evening');
    fireEvent.click(getVisibleButton('LLM设置'));
    expect(screen.getByLabelText('温度')).toHaveValue(0.4);
    fireEvent.click(getVisibleButton('保存修改'));
    await waitFor(() => expect(settingsPatch).toEqual({ expected_revision: 2, values: { chat_llm_temperature: 0.4 } }));
  });

  it('父配置重新加载未完成时禁止工作流写入，避免旧快照覆盖新默认选择', async () => {
    const second = { ...workflow, workflow_key: 'evening', display_name: '傍晚画图' };
    let settingsReads = 0;
    let finishRead: ((response: Response) => void) | undefined;
    const posts: string[] = [];
    vi.stubGlobal('fetch', vi.fn(async (url, init) => {
      const path = String(url);
      if (init?.method === 'POST') posts.push(path);
      if (path.endsWith('/settings')) {
        settingsReads += 1;
        if (settingsReads === 2) return new Promise<Response>(resolve => { finishRead = resolve; });
        return Response.json(settings);
      }
      return Response.json({ ...catalog, items: [...catalog.items, second] });
    }));
    render(<Settings token="offline-token" />);
    await openWorkflows();
    fireEvent.click(getVisibleButton(/重新加载/));
    await waitFor(() => expect(finishRead).toBeDefined());
    const selectDefault = screen.getByLabelText('将傍晚画图设为默认');
    expect(selectDefault).toBeDisabled();
    expect(getVisibleButton('同步配置目录')).toBeDisabled();
    expect(getVisibleButton('导入 JSON 文件')).toBeDisabled();
    expect(getVisibleTab('工作流管理')).toHaveAttribute('aria-disabled', 'true');
    fireEvent.click(selectDefault);
    expect(posts).toEqual([]);
    finishRead!(Response.json(settings));
    await waitFor(() => expect(selectDefault).toBeEnabled());
  });

  it('查看 JSON 使用只读内容，导出所选格式并释放下载对象', async () => {
    let downloaded: Blob | undefined;
    const create = vi.fn((blob: Blob) => { downloaded = blob; return 'blob:offline-workflow'; });
    const revoke = vi.fn();
    vi.stubGlobal('URL', class extends URL {
      static createObjectURL = create;
      static revokeObjectURL = revoke;
    });
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    vi.stubGlobal('fetch', vi.fn(async url => Response.json(String(url).endsWith('/settings')
      ? settings : String(url).endsWith('/workflows/sunrise') ? detail : catalog)));
    render(<Settings token="offline-token" />);
    await openWorkflows();
    fireEvent.click(screen.getByLabelText('查看晨光画图 JSON'));
    const dialog = await findVisibleDialog('查看工作流 JSON');
    expect(within(dialog).getByLabelText('工作流 JSON')).toHaveValue(detail.api_json);
    expect(within(dialog).getByLabelText('工作流 JSON')).toHaveAttribute('readonly');
    fireEvent.click(getVisibleTab('界面 JSON', dialog));
    expect(within(dialog).getByLabelText('工作流 JSON')).toHaveValue(detail.ui_json);
    fireEvent.click(getVisibleButton('导出当前 JSON', dialog));
    expect(revoke).toHaveBeenCalledWith('blob:offline-workflow');
    const reader = new FileReader();
    const content = await new Promise(resolve => {
      reader.onload = () => resolve(reader.result);
      reader.readAsText(downloaded!);
    });
    expect(content).toBe(detail.ui_json);
  });
});
