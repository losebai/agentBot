import { Alert, Button, Tag } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import { AiApi, changes } from '../ai-api';
import { errorMessage } from '../api';
import type { Page, Provider, ProviderDraft, ProviderProtocol } from '../ai-types';

const empty: ProviderDraft = {name:'', protocol:'openai-responses', base_url:'', credential_ref:null, enabled:false, description:''};
export function Providers({token}: {token:string}) {
  const api = useMemo(() => new AiApi(token), [token]);
  const [page, setPage] = useState<Page<Provider>>({items:[],next_cursor:null});
  const [cursor, setCursor] = useState<string>();
  const [previous, setPrevious] = useState<(string | undefined)[]>([]);
  const [editing, setEditing] = useState<Provider | null>();
  const [draft, setDraft] = useState<ProviderDraft>(empty);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const load = async () => { setBusy(true); try {setPage(await api.providers(cursor));setError('');} catch(e) {setError(errorMessage(e));} finally {setBusy(false);} };
  useEffect(() => {void load();}, [api, cursor]);
  const edit = (item: Provider | null) => {setEditing(item);setDraft(item ? {name:item.name,protocol:item.protocol,base_url:item.base_url,credential_ref:item.credential_ref,enabled:item.enabled,description:item.description} : {...empty});setError('');};
  async function save() {
    setBusy(true);setError('');
    try { if (editing) await api.updateProvider(editing.id,editing.revision,changes({name:editing.name,protocol:editing.protocol,base_url:editing.base_url,credential_ref:editing.credential_ref,enabled:editing.enabled,description:editing.description},draft));
      else await api.createProvider(draft);setEditing(undefined);await load(); }
    catch(e) {setError(errorMessage(e));} finally {setBusy(false);}
  }
  async function remove(item: Provider) {setBusy(true);try{await api.deleteProvider(item.id,item.revision);await load();}catch(e){setError(errorMessage(e));}finally{setBusy(false);}}
  return <div className="page-content"><div className="page-heading"><div><p className="eyebrow">AI PROVIDERS</p><h1>供应商管理</h1><p>配置 Python 模型服务的协议、地址与凭据引用。</p></div><Button onClick={() => edit(null)}>新增供应商</Button></div>
    {error && <Alert type="error" showIcon className="notice" message={error}/>}
    <div className="panel table-wrap"><table className="data-table"><thead><tr><th>供应商</th><th>协议 / API 根地址</th><th>凭据</th><th>状态</th><th>操作</th></tr></thead><tbody>{page.items.map(item => <tr key={item.id}><td><strong>{item.name}</strong><small>版本 {item.revision}</small></td><td>{item.protocol}<small>{item.base_url}</small></td><td>{item.credential_ref || '无凭据'}<small>{item.credential_available ? '可用' : '引用不可用'}</small></td><td><Tag color={item.enabled?'green':'default'}>{item.enabled?'启用':'停用'}</Tag></td><td><Button aria-label={`编辑${item.name}`} onClick={() => edit(item)}>编辑</Button> <Button danger disabled={busy || item.enabled} onClick={() => void remove(item)}>删除</Button></td></tr>)}</tbody></table>{!page.items.length && <p className="empty-list">暂无供应商。请先初始化 AI 数据表，再登记模型服务。</p>}</div>
    <div className="catalog-pagination"><Button disabled={!previous.length || busy} onClick={() => {setCursor(previous[previous.length-1]);setPrevious(previous.slice(0,-1));}}>上一页</Button><Button disabled={!page.next_cursor || busy} onClick={() => {setPrevious([...previous,cursor]);setCursor(page.next_cursor!);}}>下一页</Button><Button loading={busy} onClick={() => void load()}>刷新</Button></div>
    {editing !== undefined && <form className="panel catalog-form" onSubmit={e => {e.preventDefault();void save();}}><h2>{editing?'编辑供应商':'新增供应商'}</h2>
      <label htmlFor="provider-name">供应商名称</label><input id="provider-name" required maxLength={100} value={draft.name} onChange={e => setDraft({...draft,name:e.target.value})}/>
      <label htmlFor="provider-protocol">协议</label><select id="provider-protocol" value={draft.protocol} onChange={e => setDraft({...draft,protocol:e.target.value as ProviderProtocol})}><option value="openai-responses">OpenAI Responses</option><option value="openai-chat">兼容 Chat Completions</option><option value="ollama">Ollama</option></select>
      <label htmlFor="provider-url">API 根地址</label><input id="provider-url" required type="url" maxLength={2048} placeholder={draft.protocol==='ollama'?'http://127.0.0.1:11434':'https://api.example.com/v1'} value={draft.base_url} onChange={e => setDraft({...draft,base_url:e.target.value})}/>
      <label htmlFor="provider-credential">凭据环境变量</label><input id="provider-credential" placeholder="AGENT_BOT_PROVIDER_QWEN_KEY" value={draft.credential_ref || ''} onChange={e => setDraft({...draft,credential_ref:e.target.value || null})}/><p>填写运行环境中的变量名；无凭据的本机服务可留空。密钥值在服务环境中设置。</p>
      <label htmlFor="provider-description">描述</label><input id="provider-description" maxLength={1000} value={draft.description} onChange={e => setDraft({...draft,description:e.target.value})}/>
      <label className="checkbox-label"><input type="checkbox" checked={draft.enabled} onChange={e => setDraft({...draft,enabled:e.target.checked})}/>启用供应商</label>
      <p>Responses 支持输出上限、temperature、top-p。兼容协议增加 penalties 与 stop；Ollama 支持 top-k、min-p、repeat penalty 和 think。未支持参数会明确报错。</p>
      <div className="heading-actions"><Button type="primary" htmlType="submit" loading={busy}>保存供应商</Button><Button onClick={() => setEditing(undefined)}>取消</Button></div></form>}
  </div>;
}
