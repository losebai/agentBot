import { Alert, Button, Tag } from 'antd';
import { useEffect, useMemo, useRef, useState } from 'react';
import { MediaApi } from '../media-api';
import { errorMessage } from '../api';
import type { Page } from '../ai-types';
import type { MediaRun, MediaRunDetail } from '../media-types';
import { MediaAttachment } from '../components/MediaAttachment';
import { runNames } from '../components/AgentRunDetail';
import { DetailDialog } from '../components/DetailDialog';

/** 媒体记录使用弹窗查看已提交执行，查看和过期均不触发重新生成。 */
export function MediaRuns({token}:{token:string}){
  const api=useMemo(()=>new MediaApi(token),[token]);const [page,setPage]=useState<Page<MediaRun>>({items:[],next_cursor:null});
  const [cursor,setCursor]=useState<string>(),[previous,setPrevious]=useState<(string|undefined)[]>([]),[session,setSession]=useState(''),[status,setStatus]=useState('');
  const [filters,setFilters]=useState({session:'',status:''}),[detail,setDetail]=useState<MediaRunDetail>(),[error,setError]=useState(''),[busy,setBusy]=useState(false),[refresh,setRefresh]=useState(0);
  const controller=useRef<AbortController>();
  const [selected,setSelected]=useState<string>(),[detailLoading,setDetailLoading]=useState(false),[detailError,setDetailError]=useState('');
  useEffect(()=>{const abort=new AbortController();setBusy(true);api.runs(filters.session,filters.status,cursor,abort.signal).then(p=>{if(!abort.signal.aborted){setPage(p);setError('');}}).catch(e=>{if(!abort.signal.aborted)setError(errorMessage(e));}).finally(()=>{if(!abort.signal.aborted)setBusy(false);});return()=>abort.abort();},[api,filters,cursor,refresh]);
  useEffect(()=>()=>controller.current?.abort(),[api]);
  /** 关闭即取消正文请求；弹窗卸载同时释放附件请求和 Blob。 */
  function close(){controller.current?.abort();setSelected(undefined);setDetail(undefined);}
  /** 先打开弹窗，切换或关闭时旧响应不能覆盖当前选择。 */
  async function open(id:string){controller.current?.abort();const abort=new AbortController();controller.current=abort;setSelected(id);setDetail(undefined);setDetailError('');setDetailLoading(true);try{const value=await api.getRun(id,abort.signal);if(!abort.signal.aborted){setDetail(value);}}catch(e){if(!abort.signal.aborted)setDetailError(errorMessage(e));}finally{if(!abort.signal.aborted)setDetailLoading(false);}}
  return <div className="page-content"><div className="page-heading"><div><p className="eyebrow">MEDIA HISTORY</p><h1>媒体运行记录</h1><p>查看图片与对话音频的生成状态和已保存文件。</p></div><Button loading={busy} onClick={()=>setRefresh(v=>v+1)}>刷新记录</Button></div>{error&&<Alert className="notice" type="error" message={error}/>}
    <form className="record-filter" onSubmit={e=>{e.preventDefault();setCursor(undefined);setPrevious([]);setFilters({session,status});}}><label>会话 ID <input maxLength={256} value={session} onChange={e=>setSession(e.target.value)}/></label><label>状态 <select value={status} onChange={e=>setStatus(e.target.value)}><option value="">全部</option>{Object.entries(runNames).map(([key,label])=><option key={key} value={key}>{label}</option>)}</select></label><Button htmlType="submit" disabled={busy}>筛选</Button></form>
    <div className="panel table-wrap"><table className="data-table"><thead><tr><th>时间 / 会话</th><th>模型 / 类型</th><th>状态</th><th>耗时</th><th>操作</th></tr></thead><tbody>{page.items.map(run=><tr key={run.execution_id}><td>{new Date(run.created_at.endsWith('Z')?run.created_at:run.created_at+'Z').toLocaleString()}<small>{run.session_id}</small></td><td>{run.model}<small>{run.output_type==='IMAGE'?'图片':'对话音频'}</small></td><td><Tag>{runNames[run.status]}</Tag></td><td>{run.duration_ms} ms</td><td><Button onClick={()=>void open(run.execution_id)}>查看详情</Button></td></tr>)}</tbody></table>{!page.items.length&&<p className="empty-list">暂无媒体运行记录。</p>}</div>
    <div className="catalog-pagination"><Button disabled={busy||!previous.length} onClick={()=>{setCursor(previous.at(-1));setPrevious(previous.slice(0,-1));}}>上一页</Button><Button disabled={busy||!page.next_cursor} onClick={()=>{setPrevious([...previous,cursor]);setCursor(page.next_cursor!);}}>下一页</Button></div>
    {selected&&<DetailDialog title="媒体执行详情" loading={detailLoading} error={detailError} onClose={close}>{detail&&<><p>执行标识：{detail.execution_id}</p><p>{runNames[detail.status]} · {detail.error_code}</p><h3>生成描述</h3><pre>{detail.prompt}</pre>{detail.content&&<p>{detail.content}</p>}{detail.status==='running'&&<p>执行尚未确认结束，请结合服务状态检查。</p>}{detail.attachments.map(asset=><MediaAttachment key={asset.id} api={api} asset={asset}/>)}</>}</DetailDialog>}
  </div>;
}
