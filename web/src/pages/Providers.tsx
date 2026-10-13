import { Alert, Button, Tag } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import { AiApi, changes } from '../ai-api';
import { errorMessage } from '../api';
import { CredentialInput } from '../components/CredentialInput';
import { ProviderModels } from '../components/ProviderModels';
import type { Page, Provider, ProviderDraft, ProviderProtocol } from '../ai-types';

const empty: ProviderDraft = {name:'', protocol:'openai-responses', base_url:'', enabled:false, description:''};
// 多供应商独立保存 URL 和 Key，密码字段只用于写入。
export function Providers({token}: {token:string}) {
  const api = useMemo(() => new AiApi(token), [token]);
  const [page, setPage] = useState<Page<Provider>>({items:[],next_cursor:null});
  const [cursor, setCursor] = useState<string>();
  const [previous, setPrevious] = useState<(string | undefined)[]>([]);
  const [editing, setEditing] = useState<Provider | null>();
  const [draft, setDraft] = useState<ProviderDraft>(empty);
  const [discovery, setDiscovery] = useState<Provider>();
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  // 保存成功之后独立刷新目录，模型获取失败不改变保存结果。
  async function load() {
    setBusy(true);
    try {setPage(await api.providers(cursor));setError('');}
    catch(failure) {setError(errorMessage(failure));}
    finally {setBusy(false);}
  }
  useEffect(() => {void load();}, [api, cursor]);
  // 编辑从公开字段生成草稿，不读取或回填旧密钥。
  function fields(item: Provider): ProviderDraft {
    return {name:item.name,protocol:item.protocol,base_url:item.base_url,enabled:item.enabled,description:item.description};
  }
  function edit(item: Provider | null) {
    setEditing(item);setDraft(item?fields(item):{...empty});setError('');setNotice('');
  }
  // undefined 保留原密钥，null 只由明确清除操作产生。
  async function save() {
    setBusy(true);setError('');setNotice('');
    try {
      let saved:Provider;
      if(editing) {
        const patch=changes(fields(editing),draft);
        saved=Object.keys(patch).length?await api.updateProvider(editing.id,editing.revision,patch):editing;
      } else saved=await api.createProvider(draft);
      setDraft({...empty});setEditing(undefined);setNotice('供应商配置已保存。');
      setDiscovery(saved);await load();
    } catch(failure) {setError(errorMessage(failure));}
    finally {setBusy(false);}
  }
  // 删除沿用停用和版本校验，关闭该供应商的模型列表。
  async function remove(item: Provider) {
    setBusy(true);
    try {await api.deleteProvider(item.id,item.revision);if(discovery?.id===item.id)setDiscovery(undefined);await load();}
    catch(failure) {setError(errorMessage(failure));}
    finally {setBusy(false);}
  }
  return <div className="page-content"><div className="page-heading"><div><p className="eyebrow">AI PROVIDERS</p><h1>供应商管理</h1><p>每个供应商独立配置协议、API 根地址和数据库密钥。</p></div><Button disabled={busy} onClick={() => edit(null)}>新增供应商</Button></div>
    {error && <Alert type="error" showIcon className="notice" message={error}/>}
    {notice && <Alert type="success" showIcon className="notice" message={notice}/>}
    <div className="panel table-wrap"><table className="data-table"><thead><tr><th>供应商</th><th>协议 / API 根地址</th><th>凭据</th><th>状态</th><th>操作</th></tr></thead><tbody>{page.items.map(item => <tr key={item.id}><td><strong>{item.name}</strong><small>版本 {item.revision}</small></td><td>{item.protocol}<small>{item.base_url}</small></td><td>{item.credential_set?'已设置':item.credential_available?'无需凭据':'需要重新设置凭据'}</td><td><Tag color={item.enabled?'green':'default'}>{item.enabled?'启用':'停用'}</Tag></td><td><Button disabled={busy} aria-label={`编辑${item.name}`} onClick={() => edit(item)}>编辑</Button> <Button disabled={busy} aria-label={`获取${item.name}模型`} onClick={()=>setDiscovery(item)}>获取模型</Button> <Button danger disabled={busy || item.enabled} onClick={() => void remove(item)}>删除</Button></td></tr>)}</tbody></table>{!page.items.length && <p className="empty-list">暂无供应商。请新增模型服务。</p>}</div>
    <div className="catalog-pagination"><Button disabled={!previous.length || busy} onClick={() => {setCursor(previous[previous.length-1]);setPrevious(previous.slice(0,-1));}}>上一页</Button><Button disabled={!page.next_cursor || busy} onClick={() => {setPrevious([...previous,cursor]);setCursor(page.next_cursor!);}}>下一页</Button><Button loading={busy} onClick={() => void load()}>刷新</Button></div>
    {editing !== undefined && <form className="panel catalog-form" onSubmit={event => {event.preventDefault();void save();}}><h2>{editing?'编辑供应商':'新增供应商'}</h2>
      <label htmlFor="provider-name">供应商名称</label><input id="provider-name" required maxLength={100} value={draft.name} onChange={event => setDraft({...draft,name:event.target.value})}/>
      <label htmlFor="provider-protocol">协议</label><select id="provider-protocol" value={draft.protocol} onChange={event => setDraft({...draft,protocol:event.target.value as ProviderProtocol})}><option value="openai-responses">OpenAI Responses</option><option value="openai-chat">兼容 Chat Completions</option><option value="ollama">Ollama</option></select>
      <label htmlFor="provider-url">API 根地址</label><input id="provider-url" required type="url" maxLength={2048} placeholder={draft.protocol==='ollama'?'http://127.0.0.1:11434':'https://api.example.com/v1'} value={draft.base_url} onChange={event => setDraft({...draft,base_url:event.target.value})}/>
      <CredentialInput label="API 密钥" value={draft.credential} onChange={credential=>setDraft({...draft,credential})} editing={Boolean(editing)} configured={editing?.credential_set||false} available={editing?.credential_available??true}/>
      <label htmlFor="provider-description">描述</label><input id="provider-description" maxLength={1000} value={draft.description} onChange={event => setDraft({...draft,description:event.target.value})}/>
      <label className="checkbox-label"><input type="checkbox" checked={draft.enabled} onChange={event => setDraft({...draft,enabled:event.target.checked})}/>启用供应商</label>
      <p>保存后自动获取此供应商的模型列表。API 根地址不包含完整的生成端点。</p>
      <div className="heading-actions"><Button type="primary" htmlType="submit" loading={busy}>保存供应商</Button><Button onClick={() => {setEditing(undefined);setDraft({...empty});}}>取消</Button></div></form>}
    {discovery&&<ProviderModels key={`${discovery.id}:${discovery.revision}`} token={token} provider={discovery}/>}
  </div>;
}
