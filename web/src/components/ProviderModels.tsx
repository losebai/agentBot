import { Alert, Button, Input, Spin } from 'antd';
import { useEffect, useMemo, useRef, useState } from 'react';
import { AiApi } from '../ai-api';
import { errorMessage } from '../api';
import type { Provider, UpstreamModels } from '../ai-types';

// 按供应商及版本隔离发现结果，切换或退出时取消旧请求。
export function ProviderModels({token, provider, onSelect, onImported}: {
  token: string; provider: Provider; onSelect?: (model: string)=>void; onImported?: ()=>void;
}) {
  const api=useMemo(()=>new AiApi(token),[token]);
  const [result,setResult]=useState<UpstreamModels>();
  const [registered,setRegistered]=useState<Set<string>>(new Set());
  const [selected,setSelected]=useState<Record<string,string>>({});
  const [filter,setFilter]=useState('');
  const [refresh,setRefresh]=useState(0);
  const [loading,setLoading]=useState(false);
  const [importing,setImporting]=useState(false);
  const [error,setError]=useState('');
  const [notice,setNotice]=useState('');
  const controller=useRef<AbortController | undefined>(undefined);

  useEffect(()=>{
    const current=new AbortController();controller.current=current;
    setLoading(true);setResult(undefined);setSelected({});setRegistered(new Set());setError('');setNotice('');
    // 发现与本地登记状态独立读取，都必须绑定当前供应商。
    async function load() {
      try {
        const discovered=await api.upstreamModels(provider.id,provider.revision,current.signal);
        const existing=new Set<string>();let cursor:string|undefined;
        do {
          const page=await api.models(cursor,provider.id,current.signal);
          page.items.filter(item=>item.provider_id===provider.id).forEach(item=>existing.add(item.upstream_model));
          cursor=page.next_cursor||undefined;
        } while(cursor&&!current.signal.aborted);
        if(!current.signal.aborted) {setResult(discovered);setRegistered(existing);}
      } catch(failure) {if(!current.signal.aborted)setError(errorMessage(failure));}
      finally {if(!current.signal.aborted)setLoading(false);}
    }
    void load();
    return()=>current.abort();
  },[api,provider.id,provider.revision,refresh]);

  // 导入采用列表版本，过期结果不能写入已变更的供应商配置。
  async function importSelected() {
    if(!result||importing)return;
    const current=controller.current;
    if(!current||current.signal.aborted)return;
    setImporting(true);setError('');setNotice('');
    try {
      const response=await api.importModels(provider.id,result.revision,
        Object.entries(selected).map(([upstream_model,alias])=>({upstream_model,alias})),current.signal);
      if(!current.signal.aborted) {
        setNotice(`已导入 ${response.imported.length} 个模型，已登记 ${response.skipped.length} 个。新模型需在模型管理中启用。`);
        setRegistered(previous=>new Set([...previous,...Object.keys(selected)]));setSelected({});onImported?.();
      }
    } catch(failure) {if(!current.signal.aborted)setError(errorMessage(failure));}
    finally {if(!current.signal.aborted)setImporting(false);}
  }

  const entries=(result?.models||[]).filter(name=>name.toLowerCase().includes(filter.toLowerCase()));
  return <section className="panel catalog-form">
    <div className="heading-actions"><h2>{provider.name}：上游模型</h2><Button disabled={loading||importing} onClick={()=>setRefresh(refresh+1)}>刷新模型</Button></div>
    {loading&&<Spin/>}{error&&<Alert type="error" message={`获取或导入模型失败：${error}`}/>}
    {notice&&<Alert type="success" message={notice}/>}
    <Input aria-label="搜索上游模型" placeholder="搜索上游模型" value={filter} onChange={event=>setFilter(event.target.value)}/>
    {!loading&&result&&<p>共 {result.models.length} 个上游模型。列表来自该供应商的 URL 和 Key。</p>}
    <div className="table-wrap"><table className="data-table"><thead><tr><th>模型 ID</th><th>登记状态</th><th>{onSelect?'选择':'导入 / 本地别名'}</th></tr></thead><tbody>
      {entries.map(name=><tr key={name}><td>{name}</td><td>{registered.has(name)?'已登记':'未登记'}</td><td>
        {onSelect?<Button onClick={()=>onSelect(name)}>使用 {name}</Button>:<>
          <label><input type="checkbox" aria-label={`选择 ${name}`} disabled={registered.has(name)||importing}
            checked={Object.hasOwn(selected,name)} onChange={event=>setSelected(previous=>{
              const next={...previous};if(event.target.checked)next[name]=`${provider.id}/${name}`.length<=200?`${provider.id}/${name}`:'';else delete next[name];return next;
            })}/>导入</label>
          {Object.hasOwn(selected,name)&&<input aria-label={`${name} 的本地别名`} required maxLength={200}
            value={selected[name]} onChange={event=>setSelected({...selected,[name]:event.target.value})}/>}
        </>}
      </td></tr>)}
    </tbody></table></div>
    {!loading&&result&&!entries.length&&<p>没有匹配的上游模型。</p>}
    {!onSelect&&<Button type="primary" loading={importing} disabled={loading||!Object.keys(selected).length||Object.keys(selected).length>100||Object.values(selected).some(alias=>!alias.trim())} onClick={()=>void importSelected()}>导入所选模型</Button>}
  </section>;
}
