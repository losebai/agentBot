import { ChoiceSelect } from '../components/ChoiceSelect';
import { Alert, Button, Tag } from 'antd';
import { useEffect, useMemo, useRef, useState } from 'react';
import { AiApi, changes } from '../ai-api';
import { ApiFailure, errorMessage } from '../api';
import { CatalogDialog } from '../components/CatalogDialog';
import { CredentialInput } from '../components/CredentialInput';
import { Models } from './Models';
import type { CatalogPage, Provider, ProviderDraft, ProviderProtocol } from '../ai-types';

const empty: ProviderDraft = { name: '', protocol: 'openai-responses', base_url: '', user_agent: '', enabled: false, description: '' };

/** 左侧选择供应商，右侧管理其模型；供应商密钥仅在弹窗中写入，不从响应回填。 */
export function Providers({ token }: { token: string }) {
  const api = useMemo(() => new AiApi(token), [token]);
  const [page, setPage] = useState<CatalogPage<Provider>>({ items: [], next_cursor: null, total: 0 });
  const [selectedId, setSelectedId] = useState<number>();
  const [savedSelection, setSavedSelection] = useState<Provider>();
  const [editing, setEditing] = useState<Provider | null>();
  const [draft, setDraft] = useState<ProviderDraft>(empty);
  const [modelRefresh, setModelRefresh] = useState(0);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const controller = useRef<AbortController>();
  // 离开供应商页后停止模型同步，已完成的批次可在下次选择供应商时续接。
  useEffect(() => {
    const current = new AbortController(); controller.current = current;
    return () => current.abort();
  }, [api]);
  // 完整目录刷新完成前保留新记录，避免新增模型误归属首行。
  const items = savedSelection && !page.items.some(item => item.id === savedSelection.id)
    ? [...page.items, savedSelection] : page.items;
  const selected = items.find(item => item.id === selectedId) || items[0];

  /** 供应商数量有限，自动读取完整目录并交给固定高度侧栏滚动展示。 */
  async function load() {
    const signal = controller.current?.signal;
    if (!signal || signal.aborted) return;
    setBusy(true);
    try {
      const items:Provider[]=[];const ids=new Set<number>(),cursors=new Set<string>();
      let after:string|undefined;
      do {
        const next=await api.providers(after,signal);signal.throwIfAborted();
        for(const item of next.items)if(!ids.has(item.id)){ids.add(item.id);items.push(item);}
        const cursor=next.next_cursor??undefined;
        if(cursor&&cursors.has(cursor))throw new Error('供应商目录返回了重复分页游标');
        if(cursor)cursors.add(cursor);after=cursor;
      } while(after);
      setPage({items,next_cursor:null,total:items.length});
      setError('');
    }
    catch (failure) { if (!signal.aborted) setError(errorMessage(failure)); }
    finally { if (!signal.aborted) setBusy(false); }
  }
  useEffect(() => { void load(); }, [api]);

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
    return { name: item.name, protocol: item.protocol, base_url: item.base_url, user_agent: item.user_agent ?? '', enabled: item.enabled, description: item.description };
  }
  /** 新增和编辑都使用弹窗，打开时清空上次输入和提示。 */
  function edit(item: Provider | null) {
    setEditing(item); setDraft(item ? fields(item) : { ...empty }); setError(''); setNotice('');
  }
  /** 关闭时释放未提交的供应商密钥。 */
  function cancel() {
    setEditing(undefined); setDraft({ ...empty }); setError('');
  }
  /** 供应商保存成功后刷新本地目录；上游模型由管理员在模型面板显式获取。 */
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
      setDraft({ ...empty }); setEditing(undefined); setNotice('供应商已保存。');
      // 先替换当前目录的旧版本，确保新模型面板从保存响应取得同步基线。
      setPage(value => ({ ...value, items: value.items.map(item => item.id === saved.id ? saved : item) }));
      setSavedSelection(saved); setSelectedId(saved.id); setModelRefresh(value => value + 1);
      await load();
    } catch (failure) { if (!current.signal.aborted) setError(errorMessage(failure)); }
    finally { if (!current.signal.aborted) setBusy(false); }
  }
  /** 删除沿用停用及版本校验，刷新后选择目录中仍存在的供应商。 */
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
  /** 每次选择均重挂载模型面板并读取本地目录，不访问上游模型接口。 */
  function select(item: Provider) {
    setSelectedId(item.id); setModelRefresh(value => value + 1);
  }

  return <div className="page-content">
    <div className="page-heading"><div><p className="eyebrow">AI CATALOG</p><h1>供应商与模型</h1>
      <p>在左侧选择供应商，在右侧管理该供应商的模型。</p></div>
      <Button disabled={busy} onClick={() => edit(null)}>新增供应商</Button></div>
    {error && editing === undefined && <Alert type="error" showIcon className="notice" message={error} />}
    {notice && <Alert type="success" showIcon className="notice" message={notice} />}
    <div className="provider-layout">
      <aside className="panel provider-list"><div className="provider-list-heading"><h2>供应商</h2>
        <Button size="small" loading={busy} onClick={() => void load()}>刷新</Button></div>
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
      </aside>
      <section id="provider-model-panel" role="tabpanel" aria-labelledby={selected ? 'provider-tab-' + selected.id : undefined} className="provider-model-panel">
        {selected ? <Models key={selected.id + ':' + modelRefresh} token={token} provider={selected}
          onReloadProvider={signal => reloadProvider(selected.id, signal)} />
          : <div className="panel empty-list">新增供应商后，即可在这里管理模型。</div>}
      </section>
    </div>
    {editing !== undefined && <CatalogDialog title={editing ? '编辑供应商' : '新增供应商'} busy={busy} error={error} onCancel={cancel}>
      <form className="catalog-form" onSubmit={event => { event.preventDefault(); if (!busy) void save(); }}>
        <label htmlFor="provider-name">供应商名称</label><input id="provider-name" required maxLength={100} value={draft.name} onChange={event => setDraft({ ...draft, name: event.target.value })} />
        <label htmlFor="provider-protocol">协议</label><ChoiceSelect id="provider-protocol" value={draft.protocol} onChange={event => setDraft({ ...draft, protocol: event as ProviderProtocol })}>
          <option value="openai-responses">OpenAI Responses</option><option value="openai-chat">兼容 Chat Completions</option><option value="ollama">Ollama</option>
        </ChoiceSelect>
        <label htmlFor="provider-url">API 根地址</label><input id="provider-url" required type="url" maxLength={2048} placeholder={draft.protocol === 'ollama' ? 'http://127.0.0.1:11434' : 'https://api.example.com/v1'} value={draft.base_url} onChange={event => setDraft({ ...draft, base_url: event.target.value })} />
        <label htmlFor="provider-user-agent">User-Agent</label><input id="provider-user-agent" maxLength={512} placeholder="留空则使用客户端默认值" value={draft.user_agent} onChange={event => setDraft({ ...draft, user_agent: event.target.value })} />
        <CredentialInput label="API 密钥" value={draft.credential} onChange={credential => setDraft({ ...draft, credential })} editing={Boolean(editing)} configured={editing?.credential_set || false} available={editing?.credential_available ?? true} required={draft.protocol !== 'ollama'}
          onReveal={editing?.credential_set ? async () => (await api.providerCredential(editing.id, editing.revision, controller.current?.signal)).credential : undefined} />
        <label htmlFor="provider-description">描述</label><input id="provider-description" maxLength={1000} value={draft.description} onChange={event => setDraft({ ...draft, description: event.target.value })} />
        <label className="checkbox-label"><input type="checkbox" checked={draft.enabled} onChange={event => setDraft({ ...draft, enabled: event.target.checked })} />启用供应商</label>
        <p>保存后可在模型面板显式获取上游模型列表，再按需启用。API 根地址不包含完整的生成端点。</p>
        <div className="heading-actions"><Button type="primary" htmlType="submit" loading={busy}>保存供应商</Button><Button disabled={busy} onClick={cancel}>取消</Button></div>
      </form>
    </CatalogDialog>}
  </div>;
}
