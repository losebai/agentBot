import { ChoiceSelect } from './ChoiceSelect';
import { Alert, Button, Modal, Tag } from 'antd';
import { FileOutlined } from '@ant-design/icons';
import { useEffect, useMemo, useRef, useState } from 'react';
import { errorMessage } from '../api';
import type { Page } from '../ai-types';
import { MaterialLibraryApi } from '../material-library-api';
import type { LibraryMaterial, MaterialLibraryFilters } from '../material-library-types';
import { DetailDialog } from './DetailDialog';
import { MaterialThumbnail } from './MaterialThumbnail';
import { RecordPagination } from './RecordPagination';

const emptyFilters: MaterialLibraryFilters = {query: '', kind: '', entry: '', session_id: '', category: '', tag: ''};
const imageTypes = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif']);
const sources = {admin: '管理', qq: 'QQ'};
const categories = {received: '触发接收', sent: 'LLM 发送', generated: '生成图片', uploaded: '手动上传'};

/** 分类是程序登记的来源，标签可由管理员维护；旧数据不推测分类。 */
function MaterialLabels({asset}: {asset: LibraryMaterial}) {
  return <div className="material-labels">
    <div>{asset.categories.length ? asset.categories.map(category => <Tag key={category} color="blue">{categories[category]}</Tag>) : <small>未分类</small>}</div>
    <div>{asset.tags.map(tag => <Tag key={tag}>{tag}</Tag>)}</div>
  </div>;
}

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

/** 浏览和维护持久素材标签，跨来源权限限于当前管理员认证。 */
export function MaterialLibrary({token}: {token: string}) {
  const api = useMemo(() => new MaterialLibraryApi(token), [token]);
  const [filters, setFilters] = useState(emptyFilters), [draft, setDraft] = useState(emptyFilters);
  const [page, setPage] = useState<Page<LibraryMaterial>>({items: [], next_cursor: null});
  const [pageNumber, setPageNumber] = useState(1), [pageSize, setPageSize] = useState(20);
  const [total, setTotal] = useState<number | null>(null), [countError, setCountError] = useState('');
  const [loading, setLoading] = useState(true), [error, setError] = useState(''), [refresh, setRefresh] = useState(0);
  const [selected, setSelected] = useState<string>(), [detail, setDetail] = useState<LibraryMaterial>();
  const [detailLoading, setDetailLoading] = useState(false), [detailError, setDetailError] = useState('');
  const detailRequest = useRef<AbortController>();
  const mounted = useRef(true);
  const [tagEditing, setTagEditing] = useState<LibraryMaterial>();
  const [tagDraft, setTagDraft] = useState(''), [tagSaving, setTagSaving] = useState(false), [tagError, setTagError] = useState('');

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; detailRequest.current?.abort(); };
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError(''); setPage({items: [], next_cursor: null});
    detailRequest.current?.abort(); setSelected(undefined); setDetail(undefined);
    api.list(filters, undefined, controller.signal, {page: pageNumber, limit: pageSize}).then(value => {
      if (!controller.signal.aborted) setPage(value);
    }).catch(failure => { if (!controller.signal.aborted) setError(errorMessage(failure)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => { controller.abort(); detailRequest.current?.abort(); };
  }, [api, filters, pageNumber, pageSize, refresh]);

  useEffect(() => {
    const controller = new AbortController();
    setTotal(null); setCountError('');
    // 总数只随筛选和刷新重新统计，不挡住列表，也不因翻页重复扫描。
    api.count(filters, controller.signal).then(value => {
      if (!controller.signal.aborted) setTotal(value.total);
    }).catch(failure => { if (!controller.signal.aborted) setCountError(errorMessage(failure)); });
    return () => controller.abort();
  }, [api, filters, refresh]);

  useEffect(() => {
    if (total !== null) setPageNumber(current => Math.min(current, Math.max(1, Math.ceil(total / pageSize))));
  }, [total, pageSize]);

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

  /** 保存只改变标签；请求失败保留输入，提交完成后刷新当前筛选的列表和计数。 */
  async function saveTags() {
    if (!tagEditing || tagSaving) return;
    setTagSaving(true); setTagError('');
    const tags = tagDraft.split(/[,，\n]/).map(tag => tag.trim()).filter(Boolean);
    try {
      await api.updateTags(tagEditing.id, tags);
      if (mounted.current) { setTagEditing(undefined); setRefresh(value => value + 1); }
    } catch (failure) { if (mounted.current) setTagError(errorMessage(failure)); }
    finally { if (mounted.current) setTagSaving(false); }
  }

  return <div className="page-content material-library">
    <div className="page-heading"><div><p className="eyebrow">MATERIAL LIBRARY</p><h1>素材库</h1>
      <p>浏览 QQ 与管理会话保存的图片和文件，查看原件或下载。</p></div>
      <Button aria-label="刷新素材库" loading={loading} onClick={() => setRefresh(value => value + 1)}>刷新</Button></div>
    {error && <Alert className="notice" type="error" showIcon message={error} />}
    {countError && <Alert className="notice" type="error" showIcon message={countError} />}
    <form className="record-filter" onSubmit={event => {
      event.preventDefault(); setPageNumber(1); setFilters({...draft});
    }}>
      <label>关键词<input maxLength={200} value={draft.query} onChange={event => setDraft({...draft, query: event.target.value})} /></label>
      <label>素材类型<ChoiceSelect aria-label="素材类型" value={draft.kind} onChange={event => setDraft({...draft, kind: event as MaterialLibraryFilters['kind']})}>
        <option value="">全部类型</option><option value="image">图片</option><option value="file">文件</option></ChoiceSelect></label>
      <label>来源<ChoiceSelect aria-label="来源" value={draft.entry} onChange={event => setDraft({...draft, entry: event as MaterialLibraryFilters['entry']})}>
        <option value="">全部来源</option><option value="qq">QQ</option><option value="admin">管理</option></ChoiceSelect></label>
      <label>素材分类<ChoiceSelect aria-label="素材分类" value={draft.category} onChange={value => setDraft({...draft, category: value as MaterialLibraryFilters['category']})}>
        <option value="">全部分类</option>{Object.entries(categories).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</ChoiceSelect></label>
      <label>标签<input aria-label="标签筛选" maxLength={32} value={draft.tag} onChange={event => setDraft({...draft, tag: event.target.value})} /></label>
      <label>会话 ID<input maxLength={256} value={draft.session_id} onChange={event => setDraft({...draft, session_id: event.target.value})} /></label>
      <Button aria-label="筛选" htmlType="submit" disabled={loading}>筛选</Button>
    </form>
    <div className="table-wrap panel"><table className="data-table"><thead><tr>
      <th>预览</th><th>素材</th><th>类型与大小</th><th>分类与标签</th><th>来源与会话</th><th>创建时间</th><th>操作</th>
    </tr></thead><tbody>{page.items.map(asset => <tr key={asset.id}>
      <td>{asset.kind === 'image'
        ? <MaterialThumbnail api={api} asset={asset} onOpen={() => void openDetail(asset.id)} />
        : <span className="material-file-icon" aria-label="普通文件"><FileOutlined aria-hidden="true" /></span>}</td>
      <td>{asset.name}<small>{asset.description.slice(0, 120)}</small></td>
      <td>{asset.kind === 'image' ? '图片' : '文件'} · {sizeLabel(asset.size_bytes)}<small>{asset.mime_type}</small></td>
      <td><MaterialLabels asset={asset} /></td>
      <td>{sources[asset.entry]}<small>{asset.session_id}</small></td><td>{asset.created_at.replace('T', ' ')} UTC</td>
      <td><div className="material-actions"><Button aria-label={`查看${asset.name}`} onClick={() => void openDetail(asset.id)}>查看</Button>
        <Button aria-label={`编辑${asset.name}标签`} onClick={() => {
          setTagEditing(asset); setTagDraft(asset.tags.join(', ')); setTagError('');
        }}>标签</Button></div></td>
    </tr>)}</tbody></table>
      {!page.items.length && <p className="empty-list">{loading ? '正在加载…' : error ? '素材加载失败' : '暂无匹配素材'}</p>}
    </div>
    <RecordPagination current={pageNumber} pageSize={pageSize} total={total} loading={loading}
      countFailed={Boolean(countError)} hasNext={Boolean(page.next_cursor)}
      onChange={(number, size) => { setPageNumber(number); setPageSize(size); }} />
    {selected !== undefined && <DetailDialog title="素材详情" loading={detailLoading} error={detailError} onClose={closeDetail}>
      {detail && <><h3>{detail.name}</h3><p className="knowledge-owner">来源：{sources[detail.entry]} · 会话：{detail.session_id}</p>
        <MaterialLabels asset={detail} />
        <p>{detail.mime_type} · {detail.size_bytes} 字节 · {detail.created_at.replace('T', ' ')} UTC</p>
        {detail.description && <p className="knowledge-content">{detail.description}</p>}
        <p className="result-metadata">素材 ID：{detail.id}<br />SHA-256：{detail.sha256}</p>
        <MaterialPreview key={detail.id} api={api} asset={detail} /></>}
    </DetailDialog>}
    {tagEditing && <Modal open title={`编辑素材标签：${tagEditing.name}`} okText="保存标签" cancelText="取消"
      confirmLoading={tagSaving} closable={!tagSaving} maskClosable={!tagSaving} cancelButtonProps={{disabled: tagSaving}}
      onCancel={() => { if (!tagSaving) setTagEditing(undefined); }} onOk={() => void saveTags()}>
      {tagError && <Alert className="notice" type="error" showIcon message={tagError} />}
      <p>最多 8 个标签，每个 1–32 字符；用逗号分隔，留空可清除。</p>
      <label className="material-tag-editor">素材标签<input aria-label="素材标签" maxLength={512} disabled={tagSaving}
        value={tagDraft} onChange={event => setTagDraft(event.target.value)} /></label>
    </Modal>}
  </div>;
}
