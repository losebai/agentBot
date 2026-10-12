import { Alert, Button, Tag } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import { AiApi, changes } from '../ai-api';
import { errorMessage } from '../api';
import type { AiModel, ModelDraft, Page, Provider } from '../ai-types';

const empty: ModelDraft = {alias:'',upstream_model:'',provider_id:0,enabled:false};
const emptyMedia = {vision_enabled:false,image_api_profile:'none' as const,audio_chat_enabled:false};
/** 能力草稿与普通模型字段使用同一版本，冲突时保留全部输入。 */
export function Models({token}: {token:string}) {
  const api = useMemo(() => new AiApi(token), [token]);
  const [page,setPage] = useState<Page<AiModel>>({items:[],next_cursor:null});
  const [providers,setProviders] = useState<Provider[]>([]);
  const [providerCursor,setProviderCursor] = useState<string | null>(null);
  const [cursor,setCursor] = useState<string>();const [previous,setPrevious] = useState<(string | undefined)[]>([]);
  const [editing,setEditing] = useState<AiModel | null>();const [draft,setDraft] = useState(empty);
  const [error,setError] = useState('');const [busy,setBusy] = useState(false);
  const [mediaInitialized,setMediaInitialized]=useState(false);
  const editableMedia=editing?Boolean(editing.media_initialized):mediaInitialized;
  const ollama=providers.find(p=>p.id===draft.provider_id)?.protocol==='ollama';
  useEffect(()=>{let live=true;api.status().then(s=>{if(live)setMediaInitialized(Boolean(s.python_media?.initialized));}).catch(()=>{});return()=>{live=false;};},[api]);
  /** 刷新当前模型页，保留尚未提交的表单。 */
  async function load() {setBusy(true);try{setPage(await api.models(cursor));setError('');}catch(e){setError(errorMessage(e));}finally{setBusy(false);}}
  useEffect(() => {void load();},[api,cursor]);
  useEffect(() => {api.providers().then(value => {setProviders(value.items);setProviderCursor(value.next_cursor);}).catch(e => setError(errorMessage(e)));},[api]);
  /** 分页补充供应商选项。 */
  async function moreProviders() {try{const value=await api.providers(providerCursor!);setProviders([...providers,...value.items]);setProviderCursor(value.next_cursor);}catch(e){setError(errorMessage(e));}}
  /** 深拷贝能力对象，缺表时不向普通模型更新夹带媒体字段。 */
  function edit(item: AiModel | null) {setEditing(item);setDraft(item?{alias:item.alias,upstream_model:item.upstream_model,provider_id:item.provider_id,enabled:item.enabled,...(item.media_initialized?{media:{...(item.media||emptyMedia)}}:{})}:{...empty,...(mediaInitialized?{media:{...emptyMedia}}:{})});setError('');}
  /** 嵌套能力整体替换，仅真正改动时提交，失败不关闭表单。 */
  async function save() {setBusy(true);setError('');try{if(editing){const patch=changes<ModelDraft>({alias:editing.alias,upstream_model:editing.upstream_model,provider_id:editing.provider_id,enabled:editing.enabled},draft);if(JSON.stringify(draft.media)===JSON.stringify(editing.media))delete patch.media;await api.updateModel(editing.id,editing.revision,patch);}else await api.createModel(draft);setEditing(undefined);await load();}catch(e){setError(errorMessage(e));}finally{setBusy(false);}}
  /** 删除仍由服务器检查模型使用关系和版本。 */
  async function remove(item:AiModel) {setBusy(true);try{await api.deleteModel(item.id,item.revision);await load();}catch(e){setError(errorMessage(e));}finally{setBusy(false);}}
  return <div className="page-content"><div className="page-heading"><div><p className="eyebrow">MODEL CATALOG</p><h1>模型管理</h1><p>用本地别名选择模型，上游模型 ID 按原样发送。</p></div><Button onClick={() => edit(null)}>新增模型</Button></div>{error && <Alert className="notice" showIcon type="error" message={error}/>}
    <div className="panel table-wrap"><table className="data-table"><thead><tr><th>模型别名</th><th>上游模型 ID</th><th>供应商</th><th>状态</th><th>操作</th></tr></thead><tbody>{page.items.map(item => <tr key={item.id}><td><strong>{item.alias}</strong><small>版本 {item.revision}</small></td><td>{item.upstream_model}</td><td>{providers.find(p=>p.id===item.provider_id)?.name || `#${item.provider_id}`}</td><td><Tag color={item.enabled?'green':'default'}>{item.enabled?'启用':'停用'}</Tag></td><td><Button aria-label={`编辑${item.alias}`} onClick={() => edit(item)}>编辑</Button> <Button danger disabled={busy || item.enabled} onClick={() => void remove(item)}>删除</Button></td></tr>)}</tbody></table>{!page.items.length && <p className="empty-list">暂无模型。请先创建供应商，再登记普通文本模型。</p>}</div>
    <div className="catalog-pagination"><Button disabled={!previous.length||busy} onClick={()=>{setCursor(previous[previous.length-1]);setPrevious(previous.slice(0,-1));}}>上一页</Button><Button disabled={!page.next_cursor||busy} onClick={()=>{setPrevious([...previous,cursor]);setCursor(page.next_cursor!);}}>下一页</Button><Button loading={busy} onClick={()=>void load()}>刷新</Button></div>
    {editing!==undefined && <form className="panel catalog-form" onSubmit={e=>{e.preventDefault();void save();}}><h2>{editing?'编辑模型':'新增模型'}</h2><label htmlFor="model-alias">模型别名</label><input id="model-alias" required maxLength={200} value={draft.alias} onChange={e=>setDraft({...draft,alias:e.target.value})}/><label htmlFor="model-upstream">上游模型 ID</label><input id="model-upstream" required maxLength={200} value={draft.upstream_model} onChange={e=>setDraft({...draft,upstream_model:e.target.value})}/><label htmlFor="model-provider">供应商</label><select id="model-provider" required value={draft.provider_id || ''} onChange={e=>setDraft({...draft,provider_id:Number(e.target.value)})}><option value="">选择供应商</option>{providers.map(p=><option key={p.id} value={p.id}>{p.name}{p.enabled?'':'（已停用）'}</option>)}</select>{providerCursor && <Button onClick={()=>void moreProviders()}>加载更多供应商</Button>}<label className="checkbox-label"><input type="checkbox" checked={draft.enabled} onChange={e=>setDraft({...draft,enabled:e.target.checked})}/>启用模型</label><fieldset disabled={!editableMedia||busy} className="media-capabilities"><legend>媒体能力</legend>{!editableMedia&&<p>媒体表未初始化，仍可保存普通模型字段。</p>}<label className="checkbox-label"><input type="checkbox" checked={draft.media?.vision_enabled||false} onChange={e=>setDraft({...draft,media:{...(draft.media||emptyMedia),vision_enabled:e.target.checked}})}/>支持图片理解</label><label htmlFor="model-image-profile">图片生成接口</label><select id="model-image-profile" disabled={ollama||!editableMedia} value={draft.media?.image_api_profile||'none'} onChange={e=>setDraft({...draft,media:{...(draft.media||emptyMedia),image_api_profile:e.target.value as 'none'|'gpt-image'|'legacy-b64'}})}><option value="none">不支持</option><option value="gpt-image">GPT Image</option><option value="legacy-b64">兼容 Base64 图片</option></select><label className="checkbox-label"><input type="checkbox" disabled={ollama||!editableMedia} checked={draft.media?.audio_chat_enabled||false} onChange={e=>setDraft({...draft,media:{...(draft.media||emptyMedia),audio_chat_enabled:e.target.checked}})}/>支持对话音频输出</label>{ollama&&<p>Ollama 支持图片理解，不提供这里的图片生成和音频输出。</p>}</fieldset><p>在机器人配置中选择相应 Python 后端，再使用此模型别名。</p><div className="heading-actions"><Button type="primary" htmlType="submit" loading={busy}>保存模型</Button><Button onClick={()=>setEditing(undefined)}>取消</Button></div></form>}
  </div>;
}
