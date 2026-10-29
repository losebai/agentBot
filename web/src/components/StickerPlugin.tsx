import { Alert, Button, Empty, Modal, Spin, Tag } from 'antd';
import { useEffect, useMemo, useRef, useState } from 'react';
import { errorMessage } from '../api';
import { StickerApi } from '../sticker-api';
import { StickerUploadDialog } from './StickerUploadDialog';
import type { StickerMatch, StickerPage, StickerPatch, StickerView } from '../sticker-types';

/** 标签由管理员维护，中文逗号、英文逗号和换行都作为分隔；首次出现顺序保留。 */
function parseTags(value: string): string[] {
  return [...new Set(value.split(/[,，\n]/).map(tag => tag.trim()).filter(Boolean))];
}

/** 图片只使用同源认证 Blob；关闭、翻页及迟到响应均释放请求和对象 URL。 */
function StickerImage({ api, sticker, preview }: { api: StickerApi; sticker: StickerView; preview: boolean }) {
  const [url, setUrl] = useState(''), [error, setError] = useState('');
  const target = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const controller = new AbortController();
    let owned = '', disposed = false, started = false;
    setUrl(''); setError('');
    /** 缩略图接近视口才取静态 PNG，详情立即取原件以保留动画。 */
    function load() {
      if (started || disposed) return;
      started = true;
      api.content(sticker.id, preview, controller.signal).then(blob => {
        if (disposed) return;
        owned = URL.createObjectURL(blob); setUrl(owned);
      }).catch(failure => { if (!disposed) setError(errorMessage(failure)); });
    }
    const observer = preview ? new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) { observer?.disconnect(); load(); }
    }, { rootMargin: '160px' }) : undefined;
    if (observer && target.current) observer.observe(target.current);
    else load();
    return () => {
      disposed = true; observer?.disconnect(); controller.abort();
      if (owned) URL.revokeObjectURL(owned);
    };
  }, [api, sticker.id, preview]);
  return <div ref={target} className={preview ? 'sticker-preview' : 'sticker-original'}>
    {error ? <p role="alert">{error}</p> : url
      ? <img src={url} alt={`${sticker.name}${preview ? '预览' : '原件'}`} decoding="async" />
      : <span role="status">图片加载中…</span>}
    {!preview && url && <a href={url} download={sticker.name}>下载原件</a>}
  </div>;
}

type StickerDraft = { name: string; tags: string; description: string; emotion: string; usage_hint: string; enabled: boolean };

/** 编辑草稿与服务器快照分开，失败不清空输入；只提交不同字段和对应版本。 */
function StickerEditor({ api, sticker, onClose, onSaved }: {
  api: StickerApi; sticker: StickerView; onClose: () => void; onSaved: (value: StickerView) => void;
}) {
  const [draft, setDraft] = useState<StickerDraft>({ ...sticker, tags: sticker.tags.join(', ') });
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  const pending = useRef<AbortController>();
  useEffect(() => () => pending.current?.abort(), []);
  const patch: StickerPatch = { expected_revision: sticker.revision };
  for (const key of ['name', 'description', 'emotion', 'usage_hint', 'enabled'] as const) {
    if (draft[key] !== sticker[key]) Object.assign(patch, { [key]: draft[key] });
  }
  const tags = parseTags(draft.tags);
  if (JSON.stringify(tags) !== JSON.stringify(sticker.tags)) patch.tags = tags;
  const changed = Object.keys(patch).length > 1;

  /** 冲突留在当前草稿，由管理员取消并刷新后核对，绝不自动覆盖新版。 */
  async function save() {
    if (!changed || busy) return;
    const controller = new AbortController(); pending.current = controller;
    setBusy(true); setError('');
    try {
      const value = await api.update(sticker.id, patch, controller.signal);
      if (!controller.signal.aborted) onSaved(value);
    } catch (failure) { if (!controller.signal.aborted) setError(errorMessage(failure)); }
    finally { if (!controller.signal.aborted) setBusy(false); }
  }

  return <Modal open title={`编辑表情包：${sticker.name}`} okText="保存表情包" cancelText="取消" width={760}
    confirmLoading={busy} okButtonProps={{ disabled: !changed }} onOk={() => void save()} onCancel={onClose}>
    {error && <Alert className="notice" type="error" showIcon message={error} />}
    <StickerImage api={api} sticker={sticker} preview={false} />
    <p>{sticker.mime_type} · {sticker.width} × {sticker.height} · {sticker.frame_count} 帧 · {sticker.size_bytes} 字节 · 版本 {sticker.revision}</p>
    {sticker.analysis_error && <Alert className="notice" type="warning" showIcon message={`识图失败：${sticker.analysis_error}`} />}
    <fieldset className="sticker-form" disabled={busy}>
      <label>表情名称<input value={draft.name} required onChange={event => setDraft({ ...draft, name: event.target.value })} /></label>
      <label>表情标签<textarea rows={2} value={draft.tags} onChange={event => setDraft({ ...draft, tags: event.target.value })} /></label>
      <small>用逗号或换行分隔；识图不会更改自定义标签，留空清除。</small>
      <label>图片说明<textarea rows={3} value={draft.description} onChange={event => setDraft({ ...draft, description: event.target.value })} /></label>
      <label>情绪<input value={draft.emotion} onChange={event => setDraft({ ...draft, emotion: event.target.value })} /></label>
      <label>适用场景<textarea rows={2} value={draft.usage_hint} onChange={event => setDraft({ ...draft, usage_hint: event.target.value })} /></label>
      <label className="sticker-enabled"><input type="checkbox" checked={draft.enabled} onChange={event => setDraft({ ...draft, enabled: event.target.checked })} />允许机器人检索和发送</label>
    </fieldset>
  </Modal>;
}

const analysisLabels = { pending: '待识图', ready: '已识图', failed: '识图失败' };

/** 管理员维护独立表情包库；入口仅在插件实际运行时挂载，机器人使用已启用条目。 */
export function StickerPlugin({ token }: { token: string }) {
  const api = useMemo(() => new StickerApi(token), [token]);
  const [page, setPage] = useState<StickerPage>({ items: [], total: 0, next_offset: null });
  const [filters, setFilters] = useState({ query: '', tag: '' }), [filterDraft, setFilterDraft] = useState(filters);
  const [offset, setOffset] = useState(0), [refresh, setRefresh] = useState(0);
  const [loading, setLoading] = useState(true), [busy, setBusy] = useState(false);
  const [error, setError] = useState(''), [notice, setNotice] = useState('');
  const [uploadOpen, setUploadOpen] = useState(false);
  const [editing, setEditing] = useState<StickerView>();
  const [deleting, setDeleting] = useState<StickerView>();
  const [context, setContext] = useState(''), [trialTags, setTrialTags] = useState('');
  const [matches, setMatches] = useState<StickerMatch[]>();
  const pending = useRef<AbortController>();

  useEffect(() => () => pending.current?.abort(), [api]);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError(''); setPage({ items: [], total: 0, next_offset: null });
    api.list(filters.query, filters.tag, offset, controller.signal).then(value => {
      if (!controller.signal.aborted) setPage(value);
    }).catch(failure => { if (!controller.signal.aborted) setError(errorMessage(failure)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [api, filters, offset, refresh]);

  /** 所有写操作单次执行；离开插件页取消等待，不自动重复上传或识图。 */
  async function mutate(action: (signal: AbortSignal) => Promise<void>) {
    if (busy) return;
    const controller = new AbortController(); pending.current = controller;
    setBusy(true); setError(''); setNotice('');
    try { await action(controller.signal); }
    catch (failure) { if (!controller.signal.aborted) setError(errorMessage(failure)); }
    finally { if (!controller.signal.aborted) setBusy(false); }
  }

  /** 识图失败也已入库；通知包含保留状态，管理员可以手写说明或再次识图。 */
  function savedNotice(value: StickerView) {
    setNotice(value.analysis_status === 'failed'
      ? `图片已保存，识图失败：${value.analysis_error || '识图请求失败'}。可编辑说明或重识图。`
      : value.analysis_status === 'pending' ? '图片已保存，尚未识图；可手动补充说明或重识图。' : '表情包已保存。');
    setMatches(undefined); setRefresh(current => current + 1);
  }

  return <section className="sticker-library" aria-label="表情包图库">
    <div className="settings-category-heading"><h2>表情包图库</h2>
      <p>上传图片并维护标签、情绪与使用场景。机器人会按聊天语境自主挑选并发送已启用的表情包；原件保留动画。</p></div>
    {error && <Alert className="notice" type="error" showIcon message={error} />}
    {notice && <Alert className="notice" type="info" showIcon message={notice} />}
    <Button type="primary" disabled={busy} onClick={() => setUploadOpen(true)}>添加表情包</Button>
    <form className="panel sticker-trial" onSubmit={event => {
      event.preventDefault();
      void mutate(async signal => {
        const value = await api.search(context.trim(), parseTags(trialTags), signal);
        if (!signal.aborted) setMatches(value.matches);
      });
    }}><fieldset className="sticker-form" disabled={busy}>
      <legend>语境检索试用</legend><small>输入一段聊天语境，查看模型的选择和理由。试用不会发送消息。</small>
      <label>聊天语境<textarea required rows={3} value={context} onChange={event => setContext(event.target.value)} /></label>
      <label>限定标签<input value={trialTags} onChange={event => setTrialTags(event.target.value)} /></label>
      <Button htmlType="submit" loading={busy} disabled={!context.trim()}>检索表情包</Button>
    </fieldset>
      {matches && <div aria-live="polite">{!matches.length ? <p>没有匹配的表情包。</p> : matches.map(match => <div key={match.sticker.id} className="sticker-match">
        <strong>{match.sticker.name}</strong><span>匹配分数 {match.score}</span><p>{match.reason}</p>
        <Button onClick={() => setEditing(match.sticker)}>查看匹配表情包</Button></div>)}</div>}
    </form>
    <form className="record-filter" onSubmit={event => { event.preventDefault(); setOffset(0); setFilters({ query: filterDraft.query.trim(), tag: filterDraft.tag.trim() }); }}>
      <label>名称或说明<input value={filterDraft.query} onChange={event => setFilterDraft({ ...filterDraft, query: event.target.value })} /></label>
      <label>标签筛选<input value={filterDraft.tag} onChange={event => setFilterDraft({ ...filterDraft, tag: event.target.value })} /></label>
      <Button htmlType="submit" disabled={busy || loading}>筛选表情包</Button>
      <Button disabled={busy || loading} onClick={() => { setMatches(undefined); setRefresh(value => value + 1); }}>刷新图库</Button>
    </form>
    {loading ? <div className="loading"><Spin /></div> : !page.items.length ? <Empty description="暂无匹配表情包" />
      : <div className="sticker-grid">{page.items.map(item => <article className="panel sticker-card" key={item.id}>
        <StickerImage api={api} sticker={item} preview />
        <h3>{item.name}</h3><div><Tag color={item.enabled ? 'green' : undefined}>{item.enabled ? '已启用' : '已停用'}</Tag>
          <Tag color={item.analysis_status === 'failed' ? 'red' : undefined}>{analysisLabels[item.analysis_status]}</Tag>
          {item.animated && <Tag>动图</Tag>}</div>
        <div>{item.tags.map(tag => <Tag key={tag}>{tag}</Tag>)}</div>
        <p className="sticker-description">{item.description || '暂无图片说明'}</p>
        {item.analysis_error && <p className="sticker-analysis-error">{item.analysis_error}</p>}
        <div className="heading-actions">
          <Button disabled={busy} aria-label={`编辑${item.name}`} onClick={() => setEditing(item)}>查看与编辑</Button>
          <Button disabled={busy} onClick={() => void mutate(async signal => {
            const value = await api.analyze(item.id, item.revision, signal);
            if (!signal.aborted) savedNotice(value);
          })}>重识图</Button>
          <Button danger disabled={busy} aria-label={`删除${item.name}`} onClick={() => setDeleting(item)}>删除</Button>
        </div>
      </article>)}</div>}
    <div className="heading-actions sticker-pagination"><span>共 {page.total} 张 · 第 {Math.floor(offset / 20) + 1} 页</span>
      <Button disabled={!offset || loading || busy} onClick={() => setOffset(Math.max(0, offset - 20))}>上一页</Button>
      <Button disabled={page.next_offset === null || loading || busy} onClick={() => setOffset(page.next_offset!)}>下一页</Button></div>
    {uploadOpen && <StickerUploadDialog api={api}
      onClose={() => {
        // 已完成的上传不会因取消而回滚；关闭后刷新，展示已保存部分。
        setUploadOpen(false); setMatches(undefined); setOffset(0); setRefresh(value => value + 1);
      }} onUploaded={() => {
        setMatches(undefined); setOffset(0); setRefresh(value => value + 1);
      }} />}
    {editing && <StickerEditor key={editing.id} api={api} sticker={editing} onClose={() => setEditing(undefined)}
      onSaved={value => { setEditing(undefined); savedNotice(value); }} />}
    {deleting && <Modal open title={`删除表情包：${deleting.name}`} okText="确认删除" cancelText="取消"
      confirmLoading={busy} okButtonProps={{ danger: true }} onCancel={() => { if (!busy) setDeleting(undefined); }}
      onOk={() => void mutate(async signal => {
        await api.remove(deleting.id, deleting.revision, signal);
        if (!signal.aborted) {
          setDeleting(undefined); setMatches(undefined); setOffset(0);
          setNotice('表情包已删除。'); setRefresh(value => value + 1);
        }
      })}><p>删除后机器人将无法再检索和发送此图片。确认删除？</p>
      {error && <Alert type="error" showIcon message={error} />}</Modal>}
  </section>;
}
