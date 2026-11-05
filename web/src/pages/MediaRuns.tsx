import { ChoiceSelect } from '../components/ChoiceSelect';
import { Alert, Button, Tag } from 'antd';
import { useEffect, useMemo, useRef, useState } from 'react';
import { MediaApi } from '../media-api';
import { errorMessage } from '../api';
import type { CatalogPage } from '../ai-types';
import type { MediaRun, MediaRunDetail } from '../media-types';
import { MediaAttachment } from '../components/MediaAttachment';
import { runNames } from '../components/AgentRunDetail';
import { DetailDialog } from '../components/DetailDialog';
import { RecordPagination } from '../components/RecordPagination';

/** 媒体记录使用弹窗查看已提交执行，查看和过期均不触发重新生成。 */
export function MediaRuns({token}:{token:string}){
  const api=useMemo(()=>new MediaApi(token),[token]);const [page,setPage]=useState<CatalogPage<MediaRun>>({items:[],next_cursor:null,total:0});
  const [pageNumber,setPageNumber]=useState(1),[pageSize,setPageSize]=useState(10),[session,setSession]=useState(''),[status,setStatus]=useState('');
  const [filters,setFilters]=useState({session:'',status:''}),[detail,setDetail]=useState<MediaRunDetail>(),[error,setError]=useState(''),[busy,setBusy]=useState(false),[refresh,setRefresh]=useState(0);
  const controller=useRef<AbortController>();
  const [selected,setSelected]=useState<string>(),[detailLoading,setDetailLoading]=useState(false),[detailError,setDetailError]=useState('');
  useEffect(()=>{const abort=new AbortController();setBusy(true);api.runPage(filters.session,filters.status,pageNumber,pageSize,abort.signal).then(p=>{if(!abort.signal.aborted){setPage(p);setError('');const last=Math.max(1,Math.ceil(p.total/pageSize));if(pageNumber>last)setPageNumber(last);}}).catch(e=>{if(!abort.signal.aborted)setError(errorMessage(e));}).finally(()=>{if(!abort.signal.aborted)setBusy(false);});return()=>abort.abort();},[api,filters,pageNumber,pageSize,refresh]);
  useEffect(()=>()=>controller.current?.abort(),[api]);
  /** 关闭即取消正文请求；弹窗卸载同时释放附件请求和 Blob。 */
  function close(){controller.current?.abort();setSelected(undefined);setDetail(undefined);}
  /** 先打开弹窗，切换或关闭时旧响应不能覆盖当前选择。 */
  async function open(id:string){controller.current?.abort();const abort=new AbortController();controller.current=abort;setSelected(id);setDetail(undefined);setDetailError('');setDetailLoading(true);try{const value=await api.getRun(id,abort.signal);if(!abort.signal.aborted){setDetail(value);}}catch(e){if(!abort.signal.aborted)setDetailError(errorMessage(e));}finally{if(!abort.signal.aborted)setDetailLoading(false);}}
  return <div className="page-content"><div className="page-heading"><div><p className="eyebrow">MEDIA HISTORY</p><h1>媒体运行记录</h1><p>查看图片与对话音频的生成状态和已保存文件。</p></div><Button loading={busy} onClick={()=>setRefresh(v=>v+1)}>刷新记录</Button></div>{error&&<Alert className="notice" type="error" message={error}/>}
    <form className="record-filter" onSubmit={e=>{e.preventDefault();setPageNumber(1);setFilters({session,status});}}><label>会话 ID <input maxLength={256} value={session} onChange={e=>setSession(e.target.value)}/></label><label>状态 <ChoiceSelect aria-label="状态" value={status} onChange={e=>setStatus(e)}><option value="">全部</option>{Object.entries(runNames).map(([key,label])=><option key={key} value={key}>{label}</option>)}</ChoiceSelect></label><Button htmlType="submit" disabled={busy}>筛选</Button></form>
    <div className="panel table-wrap"><table className="data-table"><thead><tr><th>时间 / 会话</th><th>模型 / 类型</th><th>状态</th><th>耗时</th><th>操作</th></tr></thead><tbody>{page.items.map(run=><tr key={run.execution_id}><td>{new Date(run.created_at.endsWith('Z')?run.created_at:run.created_at+'Z').toLocaleString()}<small>{run.session_id}</small></td><td>{run.model_display_name}<small>{run.output_type==='IMAGE'?'图片':'对话音频'}</small></td><td><Tag>{runNames[run.status]}</Tag></td><td>{run.duration_ms} ms</td><td><Button onClick={()=>void open(run.execution_id)}>查看详情</Button></td></tr>)}</tbody></table>{!page.items.length&&<p className="empty-list">暂无媒体运行记录。</p>}</div>
    <RecordPagination current={pageNumber} pageSize={pageSize} total={page.total} loading={busy} hasNext={Boolean(page.next_cursor)} onChange={(number,size)=>{setPageNumber(number);setPageSize(size);}}/>
    {selected&&<DetailDialog title="媒体执行详情" loading={detailLoading} error={detailError} onClose={close}>{detail&&<><p>执行标识：{detail.execution_id}</p><p>模型：{detail.model_display_name}</p><p>{runNames[detail.status]} · {detail.error_code}</p><h3>生成描述</h3><pre>{detail.prompt}</pre>{detail.content&&<p>{detail.content}</p>}{detail.status==='running'&&<p>执行尚未确认结束，请结合服务状态检查。</p>}{detail.attachments.map(asset=><MediaAttachment key={asset.id} api={api} asset={asset}/>)}</>}</DetailDialog>}
  </div>;
}
