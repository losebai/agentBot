import { Alert, Button } from 'antd';
import { useEffect, useMemo, useRef, useState } from 'react';
import { errorMessage } from '../api';
import type { Page } from '../ai-types';
import { MaterialLibraryApi } from '../material-library-api';
import type { LibraryMaterial, MaterialLibraryFilters } from '../material-library-types';
import { DetailDialog } from './DetailDialog';

const emptyFilters: MaterialLibraryFilters = {query: '', kind: '', entry: '', session_id: ''};
const imageTypes = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif']);
const sources = {admin: '管理', qq: 'QQ'};

/** 大小仅供浏览，保留原始字节数于详情以便核对下载文件。 */
function sizeLabel(bytes: number): string {
  return bytes < 1024 * 1024 ? `${(bytes / 1024).toFixed(1)} KiB` : `${(bytes / 1024 / 1024).toFixed(1)} MiB`;
}

/** 只预览安全图片类型；关闭、卸载和迟到正文都释放本地 URL 与请求。 */
function MaterialPreview({api, asset}: {api: MaterialLibraryApi; asset: LibraryMaterial}) {
  const [url, setUrl] = useState(''), [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    let owned = '', disposed = false;
    setUrl(''); setError('');
    api.content(asset.id, controller.signal).then(blob => {
      owned = URL.createObjectURL(blob);
      if (disposed) { URL.revokeObjectURL(owned); owned = ''; }
      else setUrl(owned);
    }).catch(failure => { if (!disposed) setError(errorMessage(failure)); });
    return () => { disposed = true; controller.abort(); if (owned) URL.revokeObjectURL(owned); };
  }, [api, asset.id]);
  return <figure className="media-attachment">
    {error ? <Alert type="error" showIcon message={error} /> : !url ? <p role="status">正在读取原件…</p> : <>
      {asset.kind === 'image' && imageTypes.has(asset.mime_type)
        ? <img src={url} alt={asset.name} /> : <p>此文件仅提供原件下载。</p>}
      <a href={url} download={asset.name}>下载原件</a>
    </>}
  </figure>;
}

/** 只读浏览持久素材，跨来源权限限于当前管理员认证。 */
export function MaterialLibrary({token}: {token: string}) {
  const api = useMemo(() => new MaterialLibraryApi(token), [token]);
  const [filters, setFilters] = useState(emptyFilters), [draft, setDraft] = useState(emptyFilters);
  const [page, setPage] = useState<Page<LibraryMaterial>>({items: [], next_cursor: null});
  const [cursor, setCursor] = useState<string>(), [history, setHistory] = useState<(string | undefined)[]>([]);
  const [loading, setLoading] = useState(true), [error, setError] = useState(''), [refresh, setRefresh] = useState(0);
  const [selected, setSelected] = useState<string>(), [detail, setDetail] = useState<LibraryMaterial>();
  const [detailLoading, setDetailLoading] = useState(false), [detailError, setDetailError] = useState('');
  const detailRequest = useRef<AbortController>();

  useEffect(() => () => { detailRequest.current?.abort(); }, []);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError(''); setPage({items: [], next_cursor: null});
    detailRequest.current?.abort(); setSelected(undefined); setDetail(undefined);
    api.list(filters, cursor, controller.signal).then(value => {
      if (!controller.signal.aborted) setPage(value);
    }).catch(failure => { if (!controller.signal.aborted) setError(errorMessage(failure)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => { controller.abort(); detailRequest.current?.abort(); };
  }, [api, filters, cursor, refresh]);

  /** 关闭立即卸载预览，迟到详情不再创建内容请求或重开弹窗。 */
  function closeDetail() {
    detailRequest.current?.abort(); setSelected(undefined); setDetail(undefined);
  }

  /** 完整详情与列表请求分别取消，防止快速切换素材时交叉覆盖。 */
  async function openDetail(id: string) {
    detailRequest.current?.abort();
    const controller = new AbortController(); detailRequest.current = controller;
    setSelected(id); setDetail(undefined); setDetailLoading(true); setDetailError('');
    try {
      const value = await api.detail(id, controller.signal);
      if (!controller.signal.aborted) setDetail(value);
    } catch (failure) { if (!controller.signal.aborted) setDetailError(errorMessage(failure)); }
    finally { if (!controller.signal.aborted) setDetailLoading(false); }
  }

  return <div className="page-content">
    <div className="page-heading"><div><p className="eyebrow">MATERIAL LIBRARY</p><h1>素材库</h1>
      <p>浏览 QQ 与管理会话保存的图片和文件，查看原件或下载。</p></div>
      <Button aria-label="刷新素材库" loading={loading} onClick={() => setRefresh(value => value + 1)}>刷新</Button></div>
    {error && <Alert className="notice" type="error" showIcon message={error} />}
    <form className="record-filter" onSubmit={event => {
      event.preventDefault(); setCursor(undefined); setHistory([]); setFilters({...draft});
    }}>
      <label>关键词<input maxLength={200} value={draft.query} onChange={event => setDraft({...draft, query: event.target.value})} /></label>
      <label>素材类型<select value={draft.kind} onChange={event => setDraft({...draft, kind: event.target.value as MaterialLibraryFilters['kind']})}>
        <option value="">全部类型</option><option value="image">图片</option><option value="file">文件</option></select></label>
      <label>来源<select value={draft.entry} onChange={event => setDraft({...draft, entry: event.target.value as MaterialLibraryFilters['entry']})}>
        <option value="">全部来源</option><option value="qq">QQ</option><option value="admin">管理</option></select></label>
      <label>会话 ID<input maxLength={256} value={draft.session_id} onChange={event => setDraft({...draft, session_id: event.target.value})} /></label>
      <Button aria-label="筛选" htmlType="submit" disabled={loading}>筛选</Button>
    </form>
    <div className="table-wrap panel"><table className="data-table"><thead><tr>
      <th>素材</th><th>类型与大小</th><th>来源与会话</th><th>创建时间</th><th>操作</th>
    </tr></thead><tbody>{page.items.map(asset => <tr key={asset.id}>
      <td>{asset.name}<small>{asset.description.slice(0, 120)}</small></td>
      <td>{asset.kind === 'image' ? '图片' : '文件'} · {sizeLabel(asset.size_bytes)}<small>{asset.mime_type}</small></td>
      <td>{sources[asset.entry]}<small>{asset.session_id}</small></td><td>{asset.created_at.replace('T', ' ')} UTC</td>
      <td><Button aria-label={`查看${asset.name}`} onClick={() => void openDetail(asset.id)}>查看</Button></td>
    </tr>)}</tbody></table>
      {!page.items.length && <p className="empty-list">{loading ? '正在加载…' : error ? '素材加载失败' : '暂无匹配素材'}</p>}
    </div>
    <div className="catalog-pagination">
      <Button disabled={loading || !history.length} onClick={() => {
        setCursor(history[history.length - 1]); setHistory(value => value.slice(0, -1));
      }}>上一页</Button>
      <Button disabled={loading || !page.next_cursor} onClick={() => {
        setHistory(value => [...value, cursor]); setCursor(page.next_cursor ?? undefined);
      }}>下一页</Button>
    </div>
    {selected !== undefined && <DetailDialog title="素材详情" loading={detailLoading} error={detailError} onClose={closeDetail}>
      {detail && <><h3>{detail.name}</h3><p className="knowledge-owner">来源：{sources[detail.entry]} · 会话：{detail.session_id}</p>
        <p>{detail.mime_type} · {detail.size_bytes} 字节 · {detail.created_at.replace('T', ' ')} UTC</p>
        {detail.description && <p className="knowledge-content">{detail.description}</p>}
        <p className="result-metadata">素材 ID：{detail.id}<br />SHA-256：{detail.sha256}</p>
        <MaterialPreview key={detail.id} api={api} asset={detail} /></>}
    </DetailDialog>}
  </div>;
}
