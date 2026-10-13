import { Alert, Button, Switch } from 'antd';
import { useEffect, useMemo, useRef, useState } from 'react';
import { AiApi } from '../ai-api';
import { ApiFailure, errorMessage } from '../api';
import type { AiModel, Page, Provider } from '../ai-types';

/** 显示当前供应商的已同步模型，只切换启用状态；离开面板时取消未完成请求。 */
export function Models({ token, provider }: { token: string; provider: Provider }) {
  const api = useMemo(() => new AiApi(token), [token]);
  const [page, setPage] = useState<Page<AiModel>>({ items: [], next_cursor: null });
  const [cursor, setCursor] = useState<string>();
  const [previous, setPrevious] = useState<(string | undefined)[]>([]);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(true);
  const controller = useRef<AbortController>();

  /** 分页读取与操作后的刷新共用同一取消信号，旧供应商的结果不能覆盖新面板。 */
  async function readPage(signal: AbortSignal) {
    const next = await api.models(cursor, provider.id, signal);
    if (!signal.aborted) setPage(next);
  }

  useEffect(() => {
    const current = new AbortController();
    controller.current = current;
    setBusy(true); setError('');
    void readPage(current.signal)
      .catch(failure => { if (!current.signal.aborted) setError(errorMessage(failure)); })
      .finally(() => { if (!current.signal.aborted) setBusy(false); });
    return () => current.abort();
  }, [api, cursor, provider.id]);

  /** 刷新自动同步全部上游模型；部分批次已保存时仍显示实际目录，并保留失败提示。 */
  async function sync() {
    const current = controller.current;
    if (busy || !current || current.signal.aborted) return;
    setBusy(true); setError(''); setNotice('');
    let failureMessage = '';
    try {
      const result = await api.syncModels(provider.id, provider.revision, current.signal);
      if (!current.signal.aborted) setNotice('已同步 ' + result.total + ' 个上游模型，新增 ' + result.added + ' 个。新模型默认停用。');
    } catch (failure) {
      failureMessage = '模型同步失败：' + errorMessage(failure) + '。已保存的模型会保留，可点击刷新模型继续。';
    }
    if (current.signal.aborted) return;
    try { await readPage(current.signal); }
    catch (failure) { failureMessage += ' 列表读取失败：' + errorMessage(failure); }
    if (!current.signal.aborted) { setError(failureMessage); setBusy(false); }
  }

  /** 仅提交启用字段和版本，不覆盖别名及能力；冲突时读取真实状态供再次操作。 */
  async function toggle(item: AiModel, enabled: boolean) {
    const current = controller.current;
    if (busy || !current || current.signal.aborted) return;
    setBusy(true); setError(''); setNotice('');
    try {
      const saved = await api.updateModel(item.id, item.revision, { enabled }, current.signal);
      if (!current.signal.aborted) setPage(value => ({
        ...value, items: value.items.map(model => model.id === saved.id ? saved : model),
      }));
    } catch (failure) {
      let message = errorMessage(failure);
      if (failure instanceof ApiFailure && failure.status === 409 && !current.signal.aborted) {
        try { await readPage(current.signal); }
        catch (refreshFailure) { message += '；列表读取失败：' + errorMessage(refreshFailure); }
      }
      if (!current.signal.aborted) setError(message);
    } finally { if (!current.signal.aborted) setBusy(false); }
  }

  return <div className="provider-models">
    <div className="page-heading"><div><p className="eyebrow">MODEL CATALOG</p>
      <h2>{provider.name} 的模型</h2><p>{provider.protocol} · {provider.base_url}</p></div>
      <Button loading={busy} onClick={() => void sync()}>刷新模型</Button></div>
    {error && <Alert className="notice" showIcon type="error" message={error} />}
    {notice && <Alert className="notice" showIcon type="success" message={notice} />}
    <div className="panel table-wrap"><table className="data-table"><thead><tr>
      <th>模型 ID</th><th>模型别名</th><th>是否启用</th>
    </tr></thead><tbody>{page.items.map(item => <tr key={item.id}>
      <td><strong>{item.upstream_model}</strong></td><td>{item.alias}</td>
      <td><Switch aria-label={'启用 ' + item.alias} checked={item.enabled} disabled={busy}
        checkedChildren="启用" unCheckedChildren="停用" onChange={enabled => void toggle(item, enabled)} /></td>
    </tr>)}</tbody></table>{!page.items.length && <p className="empty-list">{busy ? '正在加载模型…' : '暂无模型，点击“刷新模型”获取供应商的全部模型。'}</p>}</div>
    <div className="catalog-pagination">
      <Button disabled={!previous.length || busy} onClick={() => { setCursor(previous.at(-1)); setPrevious(previous.slice(0, -1)); }}>上一页</Button>
      <Button disabled={!page.next_cursor || busy} onClick={() => { setPrevious([...previous, cursor]); setCursor(page.next_cursor!); }}>下一页</Button>
    </div>
  </div>;
}
