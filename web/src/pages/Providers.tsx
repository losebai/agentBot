import { Alert, Button, Tag } from 'antd';
import { useEffect, useMemo, useRef, useState } from 'react';
import { AiApi, changes } from '../ai-api';
import { ApiFailure, errorMessage } from '../api';
import { CatalogDialog } from '../components/CatalogDialog';
import { CredentialInput } from '../components/CredentialInput';
import { Models } from './Models';
import type { ModelImportItem, Page, Provider, ProviderDraft, ProviderProtocol } from '../ai-types';

const empty: ProviderDraft = { name: '', protocol: 'openai-responses', base_url: '', enabled: false, description: '' };

/** 左侧选择供应商，右侧管理其模型；供应商密钥仅在弹窗中写入，不从响应回填。 */
export function Providers({ token }: { token: string }) {
  const api = useMemo(() => new AiApi(token), [token]);
  const [page, setPage] = useState<Page<Provider>>({ items: [], next_cursor: null });
  const [cursor, setCursor] = useState<string>();
  const [previous, setPrevious] = useState<(string | undefined)[]>([]);
  const [selectedId, setSelectedId] = useState<number>();
  const [savedSelection, setSavedSelection] = useState<Provider>();
  const [editing, setEditing] = useState<Provider | null>();
  const [draft, setDraft] = useState<ProviderDraft>(empty);
  const [modelRefresh, setModelRefresh] = useState(0);
  const [modelConflicts, setModelConflicts] = useState<{ providerId: number; items: ModelImportItem[] }>();
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [noticeType, setNoticeType] = useState<'success' | 'warning'>('success');
  const [busy, setBusy] = useState(false);
  const controller = useRef<AbortController>();
  // 离开供应商页后停止模型同步，已完成的批次可在下次手动刷新时续接。
  useEffect(() => {
    const current = new AbortController(); controller.current = current;
    return () => current.abort();
  }, [api]);
  // 新保存的供应商可能不在当前分页；保留该记录，避免新增模型误归属首行。
  const items = savedSelection && !page.items.some(item => item.id === savedSelection.id)
    ? [...page.items, savedSelection] : page.items;
  const selected = items.find(item => item.id === selectedId) || items[0];

  /** 刷新供应商页，已选供应商仍存在时保持选择。 */
  async function load() {
    setBusy(true);
    try { setPage(await api.providers(cursor)); setError(''); }
    catch (failure) { setError(errorMessage(failure)); }
    finally { setBusy(false); }
  }
  useEffect(() => { void load(); }, [api, cursor]);

  /** 按身份重读最新供应商，含当前分页外的新记录；只更新快照，不重试原写入。 */
  async function reloadProvider(id: number, signal: AbortSignal) {
    setBusy(true);
    try {
      let after: string | undefined;
      let latest: Provider | undefined;
      do {
        const next = await api.providers(after, signal);
        latest = next.items.find(item => item.id === id);
        after = next.next_cursor ?? undefined;
      } while (!latest && after);
      signal.throwIfAborted();
      if (!latest) throw new ApiFailure('所选供应商已删除，请刷新供应商列表', 404, 'AI_CONFIG_NOT_FOUND');
      const saved = latest;
      setPage(value => ({ ...value, items: value.items.map(item => item.id === id ? saved : item) }));
      setSavedSelection(saved);
    } finally { if (!signal.aborted) setBusy(false); }
  }

  /** 从公开字段生成编辑草稿，保留数据库中的旧密钥。 */
  function fields(item: Provider): ProviderDraft {
    return { name: item.name, protocol: item.protocol, base_url: item.base_url, enabled: item.enabled, description: item.description };
  }
  /** 新增和编辑都使用弹窗，打开时清空上次输入和提示。 */
  function edit(item: Provider | null) {
    setEditing(item); setDraft(item ? fields(item) : { ...empty }); setError(''); setNotice(''); setNoticeType('success');
  }
  /** 关闭时释放未提交的供应商密钥。 */
  function cancel() {
    setEditing(undefined); setDraft({ ...empty }); setError('');
  }
  /** 供应商保存成功就释放密钥草稿；自动同步失败单独提示，避免重复创建供应商。 */
  async function save() {
    const current = controller.current;
    if (busy || !current || current.signal.aborted) return;
    setBusy(true); setError(''); setNotice('');
    try {
      let saved: Provider;
      if (editing) {
        const patch = changes(fields(editing), draft);
        saved = Object.keys(patch).length ? await api.updateProvider(editing.id, editing.revision, patch) : editing;
      } else saved = await api.createProvider(draft);
      if (current.signal.aborted) return;
      setDraft({ ...empty }); setEditing(undefined); setNoticeType('success'); setNotice('供应商已保存，正在同步全部模型…');
      let syncError = '';
      try {
        const result = await api.syncModels(saved.id, saved.revision, current.signal);
        if (!current.signal.aborted) {
          setModelConflicts({ providerId: saved.id, items: result.conflicts });
          setNoticeType(result.conflicts.length ? 'warning' : 'success');
          setNotice('供应商已保存，已检查 ' + result.total + ' 个模型，新增 ' + result.added + ' 个。'
            + (result.conflicts.length ? '另有 ' + result.conflicts.length + ' 个别名冲突，请在模型区处理。' : '新模型默认停用。'));
        }
      } catch (failure) {
        syncError = '供应商已保存，模型同步失败：' + errorMessage(failure) + '。可点击刷新模型继续。';
        if (!current.signal.aborted) setNotice('');
      }
      if (current.signal.aborted) return;
      setSavedSelection(saved); setSelectedId(saved.id); setModelRefresh(value => value + 1);
      await load();
      if (syncError) setError(syncError);
    } catch (failure) { if (!current.signal.aborted) setError(errorMessage(failure)); }
    finally { if (!current.signal.aborted) setBusy(false); }
  }
  /** 删除沿用停用及版本校验，刷新后选择当前页仍存在的供应商。 */
  async function remove(item: Provider) {
    setBusy(true);
    try {
      await api.deleteProvider(item.id, item.revision);
      if (savedSelection?.id === item.id) setSavedSelection(undefined);
      await load();
    }
    catch (failure) { setError(errorMessage(failure)); }
    finally { setBusy(false); }
  }
  /** 切换时卸载旧模型面板，使分页和进行中的请求只属于当前供应商。 */
  function select(item: Provider) {
    setSelectedId(item.id);
  }

  return <div className="page-content">
    <div className="page-heading"><div><p className="eyebrow">AI CATALOG</p><h1>供应商与模型</h1>
      <p>在左侧选择供应商，在右侧管理该供应商的模型。</p></div>
      <Button disabled={busy} onClick={() => edit(null)}>新增供应商</Button></div>
    {error && editing === undefined && <Alert type="error" showIcon className="notice" message={error} />}
    {notice && <Alert type={noticeType} showIcon className="notice" message={notice} />}
    <div className="provider-layout">
      <aside className="panel provider-list"><h2>供应商</h2>
        <div role="tablist" aria-label="供应商列表" aria-orientation="vertical" className="provider-tabs">
          {items.map(item => <div className="provider-item" key={item.id}>
            <button type="button" role="tab" id={'provider-tab-' + item.id} aria-controls="provider-model-panel"
              aria-selected={selected?.id === item.id} disabled={busy} onClick={() => select(item)}>
              <strong>{item.name}</strong><span>{item.protocol}</span><small>{item.base_url}</small>
              <Tag color={item.enabled ? 'green' : 'default'}>{item.enabled ? '启用' : '停用'}</Tag>
              <small>{item.credential_set ? '密钥已设置' : item.credential_available ? '无需凭据' : '需要重新设置凭据'}</small>
            </button>
            <div className="provider-item-actions"><Button size="small" disabled={busy} aria-label={'编辑' + item.name} onClick={() => edit(item)}>编辑</Button>
              <Button size="small" danger disabled={busy || item.enabled} aria-label={'删除' + item.name} onClick={() => void remove(item)}>删除</Button></div>
          </div>)}
        </div>
        {!items.length && <p className="empty-list">{busy ? '正在加载…' : '暂无供应商。请先新增供应商。'}</p>}
        <div className="catalog-pagination">
          <Button size="small" disabled={!previous.length || busy} onClick={() => { setSavedSelection(undefined); setCursor(previous.at(-1)); setPrevious(previous.slice(0, -1)); }}>上一页</Button>
          <Button size="small" disabled={!page.next_cursor || busy} onClick={() => { setSavedSelection(undefined); setPrevious([...previous, cursor]); setCursor(page.next_cursor!); }}>下一页</Button>
          <Button size="small" loading={busy} onClick={() => void load()}>刷新</Button>
        </div>
      </aside>
      <section id="provider-model-panel" role="tabpanel" aria-labelledby={selected ? 'provider-tab-' + selected.id : undefined} className="provider-model-panel">
        {selected ? <Models key={selected.id + ':' + modelRefresh} token={token} provider={selected}
          initialConflicts={modelConflicts?.providerId === selected.id ? modelConflicts.items : undefined}
          onConflictsChange={items => { setModelConflicts({ providerId: selected.id, items }); setNotice(''); }}
          onReloadProvider={signal => reloadProvider(selected.id, signal)} />
          : <div className="panel empty-list">新增供应商后，即可在这里管理模型。</div>}
      </section>
    </div>
    {editing !== undefined && <CatalogDialog title={editing ? '编辑供应商' : '新增供应商'} busy={busy} error={error} onCancel={cancel}>
      <form className="catalog-form" onSubmit={event => { event.preventDefault(); if (!busy) void save(); }}>
        <label htmlFor="provider-name">供应商名称</label><input id="provider-name" required maxLength={100} value={draft.name} onChange={event => setDraft({ ...draft, name: event.target.value })} />
        <label htmlFor="provider-protocol">协议</label><select id="provider-protocol" value={draft.protocol} onChange={event => setDraft({ ...draft, protocol: event.target.value as ProviderProtocol })}>
          <option value="openai-responses">OpenAI Responses</option><option value="openai-chat">兼容 Chat Completions</option><option value="ollama">Ollama</option>
        </select>
        <label htmlFor="provider-url">API 根地址</label><input id="provider-url" required type="url" maxLength={2048} placeholder={draft.protocol === 'ollama' ? 'http://127.0.0.1:11434' : 'https://api.example.com/v1'} value={draft.base_url} onChange={event => setDraft({ ...draft, base_url: event.target.value })} />
        <CredentialInput label="API 密钥" value={draft.credential} onChange={credential => setDraft({ ...draft, credential })} editing={Boolean(editing)} configured={editing?.credential_set || false} available={editing?.credential_available ?? true} />
        <label htmlFor="provider-description">描述</label><input id="provider-description" maxLength={1000} value={draft.description} onChange={event => setDraft({ ...draft, description: event.target.value })} />
        <label className="checkbox-label"><input type="checkbox" checked={draft.enabled} onChange={event => setDraft({ ...draft, enabled: event.target.checked })} />启用供应商</label>
        <p>保存后自动获取并保存全部模型，按需启用即可。API 根地址不包含完整的生成端点。</p>
        <div className="heading-actions"><Button type="primary" htmlType="submit" loading={busy}>保存供应商</Button><Button disabled={busy} onClick={cancel}>取消</Button></div>
      </form>
    </CatalogDialog>}
  </div>;
}
