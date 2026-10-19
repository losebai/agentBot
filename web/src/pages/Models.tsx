import { Alert, Button, Switch } from 'antd';
import { useEffect, useMemo, useRef, useState } from 'react';
import { AiApi } from '../ai-api';
import { ApiFailure, errorMessage } from '../api';
import type { AiModel, ModelDraft, ModelImportItem, Page, Provider } from '../ai-types';
import type { ModelMediaCapabilities } from '../media-types';
import { CatalogDialog } from '../components/CatalogDialog';

/** 管理模型启用、媒体能力与别名；各能力分别保存并保留其他配置。 */
export function Models({ token, provider, initialConflicts = [], onConflictsChange, onReloadProvider }: {
  token: string; provider: Provider; initialConflicts?: ModelImportItem[];
  onConflictsChange?: (items: ModelImportItem[]) => void;
  onReloadProvider?: (signal: AbortSignal) => Promise<void>;
}) {
  const api = useMemo(() => new AiApi(token), [token]);
  const [page, setPage] = useState<Page<AiModel>>({ items: [], next_cursor: null });
  const [cursor, setCursor] = useState<string>();
  const [previous, setPrevious] = useState<(string | undefined)[]>([]);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(true);
  const [editing, setEditing] = useState<AiModel | null>(null);
  const [conflicts, setConflicts] = useState(initialConflicts);
  const [importing, setImporting] = useState<ModelImportItem | null>(null);
  const [alias, setAlias] = useState('');
  const controller = useRef<AbortController>();

  /** 分页读取与操作后的刷新共用同一取消信号，旧供应商的结果不能覆盖新面板。 */
  async function readPage(signal: AbortSignal) {
    const next = await api.models(cursor, provider.id, signal);
    if (!signal.aborted) setPage(next);
  }

  useEffect(() => {
    const current = new AbortController();
    controller.current = current;
    setBusy(true); setError(''); setEditing(null); setImporting(null);
    void readPage(current.signal)
      .catch(failure => { if (!current.signal.aborted) setError(errorMessage(failure)); })
      .finally(() => { if (!current.signal.aborted) setBusy(false); });
    return () => current.abort();
  }, [api, cursor, provider.id]);

  /** 父层保留自动同步返回的待处理项，面板切换和重新挂载时不会复活已解决的冲突。 */
  function saveConflicts(items: ModelImportItem[]) {
    setConflicts(items); onConflictsChange?.(items);
  }

  /** 版本冲突只重新读取供应商，不自动重试导入，避免悄悄采用已更改的连接配置。 */
  async function syncFailure(failure: unknown, signal: AbortSignal) {
    let message = errorMessage(failure);
    if (failure instanceof ApiFailure && failure.status === 409 && failure.code === 'AI_CONFIG_CONFLICT'
      && onReloadProvider && !signal.aborted) {
      try {
        await onReloadProvider(signal);
        message += '。供应商配置已重新加载，请确认后再次刷新模型';
      } catch (refreshFailure) {
        message += '；供应商读取失败：' + errorMessage(refreshFailure);
      }
    }
    return message;
  }

  /** 刷新自动同步全部上游模型；部分批次已保存时仍显示实际目录，并保留失败提示。 */
  async function sync() {
    const current = controller.current;
    if (busy || !current || current.signal.aborted) return;
    setBusy(true); setError(''); setNotice('');
    let failureMessage = '';
    try {
      const result = await api.syncModels(provider.id, provider.revision, current.signal);
      if (!current.signal.aborted) {
        saveConflicts(result.conflicts);
        setNotice('已检查 ' + result.total + ' 个上游模型，新增 ' + result.added + ' 个。新模型默认停用。');
      }
    } catch (failure) {
      failureMessage = '模型同步失败：' + await syncFailure(failure, current.signal) + '。已保存的模型会保留。';
    }
    if (current.signal.aborted) return;
    try { await readPage(current.signal); }
    catch (failure) { failureMessage += ' 列表读取失败：' + errorMessage(failure); }
    if (!current.signal.aborted) { setError(failureMessage); setBusy(false); }
  }

  /** 冲突模型使用管理员输入的别名严格导入；再冲突时保留草稿和已保存模型。 */
  async function importConflict() {
    const current = controller.current;
    if (!importing || !alias.trim() || busy || !current || current.signal.aborted) return;
    setBusy(true); setError(''); setNotice('');
    try {
      await api.importModels(provider.id, provider.revision,
        [{ upstream_model: importing.upstream_model, alias: alias.trim() }], current.signal);
      if (current.signal.aborted) return;
      saveConflicts(conflicts.filter(item => item.upstream_model !== importing.upstream_model));
      setImporting(null); setAlias('');
      setNotice('模型已导入，新模型默认停用。');
      await readPage(current.signal);
    } catch (failure) {
      const message = await syncFailure(failure, current.signal);
      if (!current.signal.aborted) setError(message);
    } finally { if (!current.signal.aborted) setBusy(false); }
  }

  /** 仅提交目标字段及版本；冲突重新读取状态，不以失败操作覆盖数据库配置。 */
  async function updateModel(item: AiModel, changes: Partial<ModelDraft>) {
    const current = controller.current;
    if (busy || !current || current.signal.aborted) return;
    setBusy(true); setError(''); setNotice('');
    try {
      const saved = await api.updateModel(item.id, item.revision, changes, current.signal);
      if (!current.signal.aborted) {
        setPage(value => ({
          ...value, items: value.items.map(model => model.id === saved.id ? saved : model),
        }));
        if (changes.media) setNotice('媒体能力配置已保存。回到 Agent 管理后点击“刷新模型状态”即可选择。');
      }
    } catch (failure) {
      let message = errorMessage(failure);
      if (failure instanceof ApiFailure && failure.status === 409 && !current.signal.aborted) {
        try { await readPage(current.signal); }
        catch (refreshFailure) { message += '；列表读取失败：' + errorMessage(refreshFailure); }
      }
      if (!current.signal.aborted) setError(message);
    } finally { if (!current.signal.aborted) setBusy(false); }
  }

  /** 后端完整替换媒体对象，切换识图时必须保留图片生成和音频配置。 */
  async function toggleVision(item: AiModel, vision_enabled: boolean) {
    await updateModel(item, { media: {
      image_api_profile: 'none', audio_chat_enabled: false, ...item.media, vision_enabled,
    } });
  }

  /** 生图协议须显式选择，完整保留图片理解和音频能力，禁止按模型名称猜测。 */
  async function setImageProfile(item: AiModel, image_api_profile: ModelMediaCapabilities['image_api_profile']) {
    await updateModel(item, { media: {
      vision_enabled: false, audio_chat_enabled: false, ...item.media, image_api_profile,
    } });
  }

  /** 修改的是本地引用名，不改变 Agent 绑定的记录 ID；冲突时保留输入。 */
  async function saveAlias() {
    const current = controller.current;
    if (!editing || !alias.trim() || busy || !current || current.signal.aborted) return;
    setBusy(true); setError(''); setNotice('');
    try {
      const saved = await api.updateModel(editing.id, editing.revision, { alias: alias.trim() }, current.signal);
      if (!current.signal.aborted) {
        setPage(value => ({ ...value, items: value.items.map(model => model.id === saved.id ? saved : model) }));
        setEditing(null);
        setNotice('别名已更新；如机器人配置使用了旧别名，请同步修改对应模型配置。Agent 绑定无需重选。');
      }
    } catch (failure) {
      if (!current.signal.aborted) setError(errorMessage(failure));
    } finally { if (!current.signal.aborted) setBusy(false); }
  }

  return <div className="provider-models">
    <div className="page-heading"><div><p className="eyebrow">MODEL CATALOG</p>
      <h2>{provider.name} 的模型</h2><p>{provider.protocol} · {provider.base_url}</p></div>
      <Button loading={busy} onClick={() => void sync()}>刷新模型</Button></div>
    {error && !editing && !importing && <Alert className="notice" showIcon type="error" message={error} />}
    {notice && <Alert className="notice" showIcon type={conflicts.length ? 'warning' : 'success'} message={notice} />}
    {!!conflicts.length && <Alert className="notice" showIcon type="warning"
      message={conflicts.length + ' 个模型的别名已被其他记录使用，尚未导入。请设置不同别名。'}
      description={<ul>{conflicts.map(item => <li key={item.upstream_model}>
        <span>{item.upstream_model}：别名 {item.alias} 已被使用。 </span>
        <Button size="small" disabled={busy} aria-label={'设置 ' + item.upstream_model + ' 的别名并导入'}
          onClick={() => { setImporting(item); setAlias(item.alias); setError(''); }}>设置别名并导入</Button>
      </li>)}</ul>} />}
    <p className="field-help">“图片理解”开关会立即保存。请为供应商实际支持识图的模型开启，并同时启用模型与供应商。</p>
    <p className="field-help">直接生图需选择供应商支持的图片生成协议，将模型别名填入 LLM 设置的 chat_image_model，并在 Agent 允许工具中选择“模型直接生图”。</p>
    <div className="panel table-wrap"><table className="data-table"><thead><tr>
      <th>模型 ID</th><th>模型别名</th><th>是否启用</th><th>图片理解</th><th>图片生成协议</th>
    </tr></thead><tbody>{page.items.map(item => <tr key={item.id}>
      <td><strong>{item.upstream_model}</strong></td><td>{item.alias}{' '}
        <Button size="small" disabled={busy} aria-label={'修改 ' + item.alias + ' 别名'}
          onClick={() => { setEditing(item); setAlias(item.alias); setError(''); }}>修改别名</Button></td>
      <td><Switch aria-label={'启用 ' + item.alias} checked={item.enabled} disabled={busy}
        checkedChildren="启用" unCheckedChildren="停用" onChange={enabled => void updateModel(item, { enabled })} /></td>
      <td><Switch aria-label={'图片理解 ' + item.alias} checked={item.media?.vision_enabled ?? false}
        disabled={busy || item.media_initialized === false} checkedChildren="开启" unCheckedChildren="关闭"
        onChange={enabled => void toggleVision(item, enabled)} />
        {item.media_initialized === false && <small>媒体数据表未初始化</small>}</td>
      <td><select aria-label={'图片生成协议 ' + item.alias}
        value={item.media?.image_api_profile ?? 'none'}
        disabled={busy || item.media_initialized === false || provider.protocol === 'ollama'}
        onChange={event => void setImageProfile(item, event.target.value as ModelMediaCapabilities['image_api_profile'])}>
        <option value="none">不启用</option><option value="gpt-image">GPT Image</option>
        <option value="legacy-b64">兼容 Base64（PNG）</option>
      </select></td>
    </tr>)}</tbody></table>{!page.items.length && <p className="empty-list">{busy ? '正在加载模型…' : '暂无模型，点击“刷新模型”获取供应商的全部模型。'}</p>}</div>
    <div className="catalog-pagination">
      <Button disabled={!previous.length || busy} onClick={() => { setCursor(previous.at(-1)); setPrevious(previous.slice(0, -1)); }}>上一页</Button>
      <Button disabled={!page.next_cursor || busy} onClick={() => { setPrevious([...previous, cursor]); setCursor(page.next_cursor!); }}>下一页</Button>
    </div>
    {editing && <CatalogDialog title="修改模型别名" busy={busy} error={error}
      onCancel={() => { setEditing(null); setAlias(''); setError(''); }}>
      <form className="catalog-form" onSubmit={event => { event.preventDefault(); void saveAlias(); }}>
        <p>别名须全局唯一。Agent 通过记录 ID 关联；机器人配置中的旧模型别名需同步修改。</p>
        <label>模型别名<input required maxLength={200} value={alias} onChange={event => setAlias(event.target.value)} /></label>
        <Button htmlType="submit" type="primary" loading={busy} disabled={!alias.trim() || alias.trim() === editing.alias}>保存别名</Button>
      </form>
    </CatalogDialog>}
    {importing && <CatalogDialog title="设置模型别名并导入" busy={busy} error={error}
      onCancel={() => { setImporting(null); setAlias(''); setError(''); }}>
      <form className="catalog-form" onSubmit={event => { event.preventDefault(); void importConflict(); }}>
        <p>上游模型：{importing.upstream_model}。别名须全局唯一，导入后模型默认停用。</p>
        <label>模型别名<input required maxLength={200} value={alias} onChange={event => setAlias(event.target.value)} /></label>
        <Button htmlType="submit" type="primary" loading={busy}
          disabled={!alias.trim() || alias.trim() === importing.alias}>保存并导入</Button>
      </form>
    </CatalogDialog>}
  </div>;
}
