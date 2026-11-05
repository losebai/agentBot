import { ChoiceSelect } from '../components/ChoiceSelect';
import { Alert, Button, Empty, Input, Spin, Switch, Tabs, Tag } from 'antd';
import { useEffect, useMemo, useRef, useState } from 'react';
import { changes } from '../ai-api';
import { errorMessage } from '../api';
import { CatalogDialog } from '../components/CatalogDialog';
import { WorkflowApi } from '../workflow-api';
import type { WorkflowCatalog, WorkflowDetail, WorkflowMetadata, WorkflowSummary } from '../workflow-types';
import { RecordPagination } from '../components/RecordPagination';

const MAX_FILE_BYTES = 1_048_576;

/** 编辑只取允许修改的描述字段，避免把节点映射或来源路径写回服务器。 */
function metadata(item: WorkflowSummary): WorkflowMetadata {
  return { display_name: item.display_name, description: item.description,
    additional_positive_prompt: item.additional_positive_prompt,
    additional_negative_prompt: item.additional_negative_prompt };
}

/** 读取用户选择的 JSON；离开页面取消读取，并释放信号监听器。 */
function readFile(file: File, signal: AbortSignal): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    const abort = () => reader.abort();
    const release = () => signal.removeEventListener('abort', abort);
    reader.onload = () => { release(); resolve(String(reader.result)); };
    reader.onerror = () => { release(); reject(new Error('无法读取所选文件。')); };
    reader.onabort = () => { release(); reject(new DOMException('文件读取已取消', 'AbortError')); };
    signal.throwIfAborted();
    signal.addEventListener('abort', abort, { once: true });
    reader.readAsText(file);
  });
}

/** 按需挂载的工作流目录；配置草稿由父页面持有，目录操作独立立即保存。 */
export function ComfyWorkflows({ token, disabled, onSettingsChanged, onBusyChange }: {
  token: string;
  disabled: boolean;
  onSettingsChanged: (defaultKey: string) => Promise<void>;
  onBusyChange: (busy: boolean) => void;
}) {
  const api = useMemo(() => new WorkflowApi(token), [token]);
  const controller = useRef<AbortController>();
  const fileInput = useRef<HTMLInputElement>(null);
  const [catalog, setCatalog] = useState<WorkflowCatalog>();
  const [loading, setLoading] = useState(true);
  const [pageNumber,setPageNumber]=useState(1),[pageSize,setPageSize]=useState(10);
  const [busy, setBusy] = useState(false);
  const [filter, setFilter] = useState('');
  const [status, setStatus] = useState('all');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [editing, setEditing] = useState<WorkflowSummary>();
  const [draft, setDraft] = useState<WorkflowMetadata>();
  const [editNotice, setEditNotice] = useState('');
  const [preview, setPreview] = useState<WorkflowDetail>();
  const [jsonFormat, setJsonFormat] = useState<'api' | 'ui'>('api');
  const blocked = busy || loading || disabled;

  /** 读取最新目录，失败交给调用者显示，避免保存成功后静默留下过时列表。 */
  async function reload(signal: AbortSignal) {
    const result = await api.page(pageNumber, pageSize, signal);
    signal.throwIfAborted();
    setCatalog(result);
    const last=Math.max(1,Math.ceil(result.total/pageSize));if(pageNumber>last)setPageNumber(last);
  }

  useEffect(() => {
    const current = new AbortController();
    controller.current = current;
    setLoading(true);
    void reload(current.signal).catch(failure => {
      if (!current.signal.aborted) setError(errorMessage(failure));
    }).finally(() => { if (!current.signal.aborted) setLoading(false); });
    // 页面离开后停止等待 HTTP 与文件读取；已保存的修改由下次目录读取反映。
    return () => current.abort();
  }, [api,pageNumber,pageSize]);
  useEffect(() => {
    onBusyChange(busy);
    return () => onBusyChange(false);
  }, [busy, onBusyChange]);

  /** 明确区分写入失败和写入后的刷新失败；冲突时不清除编辑草稿。 */
  async function mutate(request: (signal: AbortSignal) => Promise<string>, afterSaved?: () => void | Promise<void>) {
    if (blocked) return;
    const signal = controller.current!.signal;
    setBusy(true); setError(''); setNotice('');
    let committed = false;
    try {
      const message = await request(signal);
      signal.throwIfAborted();
      committed = true; setNotice(message);
      await afterSaved?.();
      await reload(signal);
    } catch (failure) {
      if (!signal.aborted) setError((committed ? '修改已保存，但刷新失败：' : '') + errorMessage(failure));
    } finally { if (!signal.aborted) setBusy(false); }
  }

  /** 先限制选择文件大小与 JSON 格式，再提交内容；浏览器不传本地路径。 */
  async function importFile(file: File | undefined) {
    if (!file || blocked) return;
    if (!file.name.toLowerCase().endsWith('.json')) { setError('请选择 JSON 文件。'); return; }
    if (file.size > MAX_FILE_BYTES) { setError('JSON 文件不能超过 1 MiB。'); return; }
    await mutate(async signal => {
      const content = await readFile(file, signal);
      try { JSON.parse(content); } catch { throw new Error('文件内容不是有效 JSON。'); }
      const result = await api.importFile(file.name, content, signal);
      return `已导入 ${result.imported} 个工作流。`;
    });
  }

  /** 编辑时仅复制公开元数据，取消会丢弃本弹窗的草稿。 */
  function edit(item: WorkflowSummary) {
    setError(''); setNotice(''); setEditNotice(''); setEditing(item); setDraft(metadata(item));
  }

  /** 显式更新编辑基线，只把用户已改字段合并到最新元数据，随后复核再保存。 */
  async function reloadEditing() {
    if (blocked || !editing || !draft) return;
    const dirty = changes(metadata(editing), draft);
    const signal = controller.current!.signal;
    setBusy(true); setError(''); setEditNotice('');
    try {
      const latest = await api.detail(editing.workflow_key, signal);
      signal.throwIfAborted();
      setEditing(latest); setDraft({ ...metadata(latest), ...dirty });
      setEditNotice('已读取最新版本并保留修改，请复核后再保存。');
    } catch (failure) {
      // 读取失败保留原版本与草稿，不能将未知版本当作新的写入基线。
      if (!signal.aborted) setError(errorMessage(failure));
    } finally { if (!signal.aborted) setBusy(false); }
  }

  /** 只提交真实修改和旧版本；保存成功才关闭弹窗并刷新目录。 */
  async function saveMetadata() {
    if (blocked || !editing || !draft) return;
    setEditNotice('');
    const patch = changes(metadata(editing), draft);
    if (!Object.keys(patch).length) { setEditing(undefined); setDraft(undefined); return; }
    await mutate(async signal => {
      await api.update(editing.workflow_key, editing.revision, patch, signal);
      return '工作流已保存。';
    }, () => { setEditing(undefined); setDraft(undefined); });
  }

  /** 默认选择刷新父配置版本，父组件移除旧默认键草稿并保留其他分类草稿。 */
  async function setDefault(item: WorkflowSummary) {
    if (!catalog) return;
    await mutate(async signal => {
      const result = await api.setDefault(item.workflow_key, catalog.settings_revision, signal);
      signal.throwIfAborted();
      setCatalog(result);
      return '默认工作流已更新。';
    }, () => onSettingsChanged(item.workflow_key));
  }

  /** JSON 预览独立读取，不把完整文件或源路径放进目录行。 */
  async function showJson(item: WorkflowSummary) {
    if (blocked) return;
    const signal = controller.current!.signal;
    setBusy(true); setError('');
    try {
      const detail = await api.detail(item.workflow_key, signal);
      signal.throwIfAborted();
      setJsonFormat(detail.api_json !== null ? 'api' : 'ui'); setPreview(detail);
    } catch (failure) { if (!signal.aborted) setError(errorMessage(failure)); }
    finally { if (!signal.aborted) setBusy(false); }
  }

  /** 下载用户选中的原始 JSON，完成触发后立即释放对象 URL。 */
  function exportJson() {
    if (!preview) return;
    const content = jsonFormat === 'api' ? preview.api_json : preview.ui_json;
    if (content === null) return;
    const url = URL.createObjectURL(new Blob([content], { type: 'application/json;charset=utf-8' }));
    const link = document.createElement('a');
    try {
      link.href = url; link.download = `${preview.workflow_key}-${jsonFormat}.json`;
      document.body.appendChild(link); link.click();
    } finally { link.remove(); URL.revokeObjectURL(url); }
  }

  /** 手动刷新不更改父配置草稿，读取失败时保留当前列表供用户判断。 */
  async function refresh() {
    if (blocked) return;
    const signal = controller.current!.signal;
    setLoading(true); setError('');
    try { await reload(signal); }
    catch (failure) { if (!signal.aborted) setError(errorMessage(failure)); }
    finally { if (!signal.aborted) setLoading(false); }
  }

  const items = catalog?.items.filter(item => `${item.display_name} ${item.description}`.toLowerCase().includes(filter.toLowerCase())
    && (status === 'all' || (status === 'enabled' ? item.enabled : !item.enabled))) || [];
  const jsonContent = preview ? jsonFormat === 'api' ? preview.api_json : preview.ui_json : null;
  return <div className="workflow-management">
    <p className="workflow-note">工作流操作立即保存；导入 ComfyUI 导出的 JSON 文件，适配后才可启用。</p>
    {error && !editing && <Alert type="error" showIcon className="notice" message={error} />}
    {notice && <Alert type="success" showIcon className="notice" message={notice} />}
    <div className="workflow-toolbar">
      <Input aria-label="筛选工作流" placeholder="筛选已加载的名称或说明" value={filter} onChange={event => setFilter(event.target.value)} />
      <ChoiceSelect aria-label="工作流状态" value={status} onChange={event => setStatus(event)}>
        <option value="all">全部状态</option><option value="enabled">已启用</option><option value="disabled">已停用</option>
      </ChoiceSelect>
      <Button disabled={blocked} onClick={() => fileInput.current?.click()}>导入 JSON 文件</Button>
      <input ref={fileInput} aria-label="导入 JSON 文件" type="file" accept=".json,application/json" hidden disabled={blocked}
        onChange={event => { const file = event.target.files?.[0]; event.target.value = ''; void importFile(file); }} />
      <Button disabled={blocked || !catalog?.directory_configured} onClick={() => void mutate(async signal => {
        const result = await api.sync(signal); return `已同步 ${result.imported} 个工作流。`;
      })}>同步配置目录</Button>
      <Button disabled={busy || disabled} loading={loading} onClick={() => void refresh()}>刷新工作流</Button>
    </div>
    {catalog && <div className="workflow-pagination-info"><span>共 {catalog.total} 个工作流</span><span>筛选当前页工作流</span></div>}
    {catalog && !catalog.directory_configured && <p className="workflow-note">尚未配置工作流目录，可先导入 JSON 文件；配置目录后重启服务即可同步。</p>}
    {loading ? <div className="loading"><Spin /></div> : <div className="workflow-list">
      {!items.length && <Empty description={catalog?.items.length ? '没有符合筛选条件的工作流' : '暂无工作流，请导入 JSON 文件或同步配置目录'} />}
      {items.map(item => <article className="workflow-item" key={item.workflow_key}>
        <div className="workflow-info"><h3>{item.display_name}</h3>
          <div className="workflow-tags">
            {catalog?.default_key === item.workflow_key && <Tag color="green">默认工作流</Tag>}
            <Tag>{item.source_format === 'ui' ? 'ComfyUI 界面格式' : '执行格式'}</Tag>
            {item.workflow_type === 'image_to_video' && <Tag color="blue">图生视频</Tag>}
            <Tag color={item.executable ? 'default' : 'gold'}>{item.executable ? '可运行' : '待适配'}</Tag>
          </div>
          {item.description && <p>{item.description}</p>}
        </div>
        <div className="workflow-actions">
          <Switch checked={item.enabled} aria-label={`启用${item.display_name}`} disabled={blocked || (!item.executable && !item.enabled)}
            onChange={enabled => void mutate(async signal => {
              await api.update(item.workflow_key, item.revision, { enabled }, signal);
              return enabled ? '工作流已启用。' : '工作流已停用。';
            })} />
          <span>{item.enabled ? '已启用' : '已停用'}</span>
          <Button size="small" disabled={blocked || item.workflow_type === 'image_to_video' || !item.enabled || !item.executable || catalog?.default_key === item.workflow_key}
            aria-label={`将${item.display_name}设为默认`} onClick={() => void setDefault(item)}>设为默认</Button>
          <Button size="small" disabled={blocked} aria-label={`编辑${item.display_name}`} onClick={() => edit(item)}>编辑</Button>
          <Button size="small" disabled={blocked} aria-label={`查看${item.display_name} JSON`} onClick={() => void showJson(item)}>查看 JSON</Button>
        </div>
      </article>)}
    </div>}
    {catalog && <RecordPagination current={pageNumber} pageSize={pageSize} total={catalog.total}
      loading={loading} hasNext={Boolean(catalog.next_cursor)}
      onChange={(number,size)=>{setPageNumber(number);setPageSize(size);}}/>}
    {editing && draft && <CatalogDialog title="编辑工作流" busy={busy} error={error}
      onCancel={() => { setEditing(undefined); setDraft(undefined); setError(''); }}>
      {error && <Button className="notice" disabled={blocked} onClick={() => void reloadEditing()}>读取最新版本并保留修改</Button>}
      {editNotice && <Alert type="info" showIcon className="notice" message={editNotice} />}
      <form className="catalog-form" onSubmit={event => { event.preventDefault(); void saveMetadata(); }}>
        <label htmlFor="workflow-name">显示名称</label>
        <Input id="workflow-name" disabled={busy} required maxLength={255} value={draft.display_name} onChange={event => setDraft({ ...draft, display_name: event.target.value })} />
        <label htmlFor="workflow-description">说明</label>
        <Input.TextArea id="workflow-description" disabled={busy} maxLength={2000} value={draft.description} onChange={event => setDraft({ ...draft, description: event.target.value })} />
        <label htmlFor="workflow-positive">附加正向提示词</label>
        <Input.TextArea id="workflow-positive" disabled={busy} maxLength={2000} value={draft.additional_positive_prompt} onChange={event => setDraft({ ...draft, additional_positive_prompt: event.target.value })} />
        <label htmlFor="workflow-negative">附加负向提示词</label>
        <Input.TextArea id="workflow-negative" disabled={busy || editing?.workflow_type === 'image_to_video'} maxLength={2000} value={draft.additional_negative_prompt} onChange={event => setDraft({ ...draft, additional_negative_prompt: event.target.value })} />
        <div className="heading-actions"><Button aria-label="取消" disabled={busy} onClick={() => { setEditing(undefined); setDraft(undefined); setError(''); }}>取消</Button>
          <Button type="primary" htmlType="submit" disabled={blocked} loading={busy}>保存工作流</Button></div>
      </form>
    </CatalogDialog>}
    {preview && <CatalogDialog title="查看工作流 JSON" busy={false} onCancel={() => setPreview(undefined)}>
      <p className="workflow-note">{preview.display_name} · 只读预览，可导出当前格式。</p>
      <Tabs activeKey={jsonFormat} onChange={key => setJsonFormat(key as 'api' | 'ui')} items={[
        { key: 'api', label: '执行 JSON', disabled: preview.api_json === null },
        { key: 'ui', label: '界面 JSON', disabled: preview.ui_json === null },
      ]} />
      {jsonContent === null ? <Empty description="此工作流没有可查看的 JSON" />
        : <Input.TextArea className="workflow-json" aria-label="工作流 JSON" readOnly value={jsonContent} autoSize={{ minRows: 8, maxRows: 20 }} />}
      <div className="heading-actions"><Button onClick={() => setPreview(undefined)}>关闭</Button>
        <Button disabled={jsonContent === null} onClick={exportJson}>导出当前 JSON</Button></div>
    </CatalogDialog>}
  </div>;
}
