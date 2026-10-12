import { Alert, Button, Tag } from 'antd';
import { useEffect, useState } from 'react';
import type { Dispatch, ReactNode, SetStateAction } from 'react';
import type { Page } from '../ai-types';
import type { DirectoryDraft, Versioned } from '../agent-types';
import { errorMessage } from '../api';

export interface DirectoryActions<D, V> {
  list(cursor?: string): Promise<Page<V>>;
  create(draft: D): Promise<V>;
  update(id: number, revision: number, draft: Partial<D>): Promise<V>;
  remove(id: number, revision: number): Promise<void>;
}
export function dirtyFields<D extends object>(original: D, draft: D): Partial<D> {
  const result: Partial<D>={};
  for(const key of Object.keys(draft) as (keyof D)[]) if(JSON.stringify(original[key])!==JSON.stringify(draft[key])) result[key]=draft[key];
  return result;
}
export const toolNames = (text: string) => text.split(/[\n,，]/).map(s=>s.trim()).filter(Boolean);

export function ToolNamesInput({label,value,onChange,required=false}:{label:string;value:string[];onChange:(value:string[])=>void;required?:boolean}) {
  const [text,setText]=useState(value.join('\n'));
  useEffect(()=>{if(JSON.stringify(toolNames(text))!==JSON.stringify(value))setText(value.join('\n'));},[value]);
  return <label>{label}<textarea required={required} value={text} onChange={e=>{setText(e.target.value);onChange(toolNames(e.target.value));}}/></label>;
}

export function DirectoryEditor<D extends DirectoryDraft,V extends D & Versioned>({title,label,description,empty,actions,fields,valid=()=>true}: {
  title:string;label:string;description:string;empty:D;actions:DirectoryActions<D,V>;
  fields:(draft:D,set:Dispatch<SetStateAction<D>>,editing:V|null)=>ReactNode;valid?:(draft:D)=>boolean;
}) {
  const [page,setPage]=useState<Page<V>>({items:[],next_cursor:null});
  const [cursor,setCursor]=useState<string>();const [previous,setPrevious]=useState<(string|undefined)[]>([]);
  const [editing,setEditing]=useState<V|null>();const [draft,setDraft]=useState<D>(empty);
  const [error,setError]=useState('');const [busy,setBusy]=useState(false);
  async function load(){setBusy(true);try{setPage(await actions.list(cursor));setError('');}catch(e){setError(errorMessage(e));}finally{setBusy(false);}}
  useEffect(()=>{void load();},[actions,cursor]);
  function edit(item:V|null){setEditing(item);const next={...empty};for(const key of Object.keys(empty) as (keyof D)[]) if(item)next[key]=item[key];setDraft(next);setError('');}
  async function save(){setBusy(true);setError('');try{if(editing){const original={...empty};for(const key of Object.keys(empty) as (keyof D)[])original[key]=editing[key];const change=dirtyFields(original,draft);if(Object.keys(change).length)await actions.update(editing.id,editing.revision,change);}else await actions.create(draft);setEditing(undefined);await load();}catch(e){setError(errorMessage(e));}finally{setBusy(false);}}
  async function remove(item:V){setBusy(true);try{await actions.remove(item.id,item.revision);await load();}catch(e){setError(errorMessage(e));}finally{setBusy(false);}}
  return <div className="page-content"><div className="page-heading"><div><p className="eyebrow">AGENT WORKSPACE</p><h1>{title}</h1><p>{description}</p></div><Button disabled={busy} onClick={()=>edit(null)}>新增 {label}</Button></div>
    {error&&<Alert className="notice" type="error" showIcon message={error}/>}
    <div className="panel table-wrap"><table className="data-table"><thead><tr><th>名称</th><th>说明</th><th>状态</th><th>操作</th></tr></thead><tbody>{page.items.map(item=><tr key={item.id}><td><strong>{item.name}</strong><small>#{item.id} · 版本 {item.revision}</small></td><td>{item.description||'—'}</td><td><Tag color={item.enabled?'green':'default'}>{item.enabled?'启用':'停用'}</Tag></td><td><Button disabled={busy} aria-label={`编辑${item.name}`} onClick={()=>edit(item)}>编辑</Button> <Button danger disabled={busy||item.enabled} onClick={()=>void remove(item)}>删除</Button></td></tr>)}</tbody></table>{!page.items.length&&<p className="empty-list">{busy?'正在加载…':`暂无 ${label}。创建后启用即可使用。`}</p>}</div>
    <div className="catalog-pagination"><Button disabled={busy||!previous.length} onClick={()=>{setCursor(previous.at(-1));setPrevious(previous.slice(0,-1));}}>上一页</Button><Button disabled={busy||!page.next_cursor} onClick={()=>{setPrevious([...previous,cursor]);setCursor(page.next_cursor!);}}>下一页</Button><Button loading={busy} onClick={()=>void load()}>刷新</Button></div>
    {editing!==undefined&&<form key={editing?.id??'new'} className="panel catalog-form" onSubmit={e=>{e.preventDefault();if(valid(draft))void save();}}><h2>{editing?'编辑':'新增'} {label}</h2><label>名称<input required maxLength={200} value={draft.name} onChange={e=>setDraft({...draft,name:e.target.value})}/></label><label>说明<textarea maxLength={2000} value={draft.description} onChange={e=>setDraft({...draft,description:e.target.value})}/></label>
      {fields(draft,setDraft,editing)}<label className="checkbox-label"><input type="checkbox" checked={draft.enabled} onChange={e=>setDraft({...draft,enabled:e.target.checked})}/>启用 {label}</label>
      <div className="heading-actions"><Button type="primary" htmlType="submit" disabled={!valid(draft)} loading={busy}>保存 {label}</Button><Button disabled={busy} onClick={()=>setEditing(undefined)}>取消</Button></div></form>}
  </div>;
}
