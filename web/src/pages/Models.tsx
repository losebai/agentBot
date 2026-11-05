import { CloudDownloadOutlined } from '@ant-design/icons';
import { Alert, Button, Pagination, Switch } from 'antd';
import { useEffect, useMemo, useRef, useState } from 'react';
import { AiApi } from '../ai-api';
import { ApiFailure, errorMessage } from '../api';
import type { AiModel, CatalogPage, ModelDraft, Provider } from '../ai-types';
import type { ModelMediaCapabilities } from '../media-types';
import { ChoiceSelect } from '../components/ChoiceSelect';

/** 管理供应商模型的启用与媒体能力，模型身份由供应商和上游标识确定。 */
export function Models({ token, provider, onReloadProvider }: {
  token: string; provider: Provider;
  onReloadProvider?: (signal: AbortSignal) => Promise<void>;
}) {
  const api = useMemo(() => new AiApi(token), [token]);
  const [page, setPage] = useState<CatalogPage<AiModel>>({ items: [], next_cursor: null, total: 0 });
  const [pageNumber, setPageNumber] = useState(1);
  const [pageSize, setPageSize] = useState(10);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(true);
  const controller = useRef<AbortController>();

  /** 分页读取与操作后的刷新共用同一取消信号，旧供应商的结果不能覆盖新面板。 */
  async function readPage(signal: AbortSignal) {
    const next = await api.modelPage(provider.id, pageNumber, pageSize, signal);
    if (!signal.aborted) {
      const lastPage = Math.max(1, Math.ceil(next.total / pageSize));
      if (pageNumber > lastPage) setPageNumber(lastPage);
      else setPage(next);
    }
  }

  useEffect(() => {
    const current = new AbortController();
    controller.current = current;
    setBusy(true); setError('');
    // 打开或切换供应商仅读取已保存目录，上游发现必须由管理员显式触发。
    void readPage(current.signal)
      .catch(failure => { if (!current.signal.aborted) setError(errorMessage(failure)); })
      .finally(() => { if (!current.signal.aborted) setBusy(false); });
    return () => current.abort();
  }, [api, pageNumber, pageSize, provider.id]);

  /** 版本冲突只重新读取供应商，不自动重试导入，避免悄悄采用已更改的连接配置。 */
  async function syncFailure(failure: unknown, signal: AbortSignal) {
    let message = errorMessage(failure);
    if (failure instanceof ApiFailure && failure.status === 409 && failure.code === 'AI_CONFIG_CONFLICT'
      && onReloadProvider && !signal.aborted) {
      try {
        await onReloadProvider(signal);
        message += '。供应商配置已重新加载，请确认后重新选择该供应商';
      } catch (refreshFailure) {
        message += '；供应商读取失败：' + errorMessage(refreshFailure);
      }
    }
    return message;
  }

  /** 管理员显式同步上游目录；失败仍读取已保存目录，不自动重试写入。 */
  async function sync(signal: AbortSignal) {
    setNotice('');
    let failureMessage = '';
    try {
      const result = await api.syncModels(provider.id, provider.revision, signal);
      if (!signal.aborted) {
        setNotice('上游 /models 返回 ' + result.total + ' 个模型，新增 ' + result.added + ' 个。新模型默认停用。');
      }
    } catch (failure) {
      failureMessage = '模型同步失败：' + await syncFailure(failure, signal) + '。已保存的模型会保留。';
    }
    if (signal.aborted) return;
    try { await readPage(signal); }
    catch (failure) { failureMessage += ' 列表读取失败：' + errorMessage(failure); }
    if (!signal.aborted) setError(failureMessage);
  }

  /** 管理员显式重新获取上游目录，补充缺失模型后刷新当前本地分页。 */
  async function refreshUpstreamModels() {
    const current = controller.current;
    if (busy || !current || current.signal.aborted) return;
    setBusy(true); setError(''); setNotice('');
    try { await sync(current.signal); }
    finally { if (!current.signal.aborted) setBusy(false); }
  }

  /** 仅提交目标字段及版本；冲突重新读取状态，不以失败操作覆盖数据库配置。 */
  async function updateModel(item: AiModel, changes: Partial<ModelDraft>) {
    const current = controller.current;
    if (busy || !current || current.signal.aborted) return;
    setBusy(true); setError(''); setNotice('');
    try {
      const saved = await api.updateModel(item.id, item.revision, changes, current.signal);
      if (!current.signal.aborted) {
        setPage(value => ({
          ...value, items: value.items.map(model => model.id === saved.id ? saved : model),
        }));
        if (changes.media) setNotice('媒体能力配置已保存。重新打开模型选择框即可获取最新状态。');
      }
    } catch (failure) {
      let message = errorMessage(failure);
      if (failure instanceof ApiFailure && failure.status === 409 && !current.signal.aborted) {
        try { await readPage(current.signal); }
        catch (refreshFailure) { message += '；列表读取失败：' + errorMessage(refreshFailure); }
      }
      if (!current.signal.aborted) setError(message);
    } finally { if (!current.signal.aborted) setBusy(false); }
  }

  /** 后端完整替换媒体对象，切换识图时必须保留图片生成和音频配置。 */
  async function toggleVision(item: AiModel, vision_enabled: boolean) {
    await updateModel(item, { media: {
      image_api_profile: 'none', audio_chat_enabled: false, ...item.media, vision_enabled,
    } });
  }

  /** 生图协议须显式选择，完整保留图片理解和音频能力，禁止按模型名称猜测。 */
  async function setImageProfile(item: AiModel, image_api_profile: ModelMediaCapabilities['image_api_profile']) {
    await updateModel(item, { media: {
      vision_enabled: false, audio_chat_enabled: false, ...item.media, image_api_profile,
    } });
  }

  return <div className="provider-models">
    <div className="page-heading"><div><p className="eyebrow">MODEL CATALOG</p>
      <h2>{provider.name} 的模型</h2><p>{provider.protocol} · {provider.base_url}</p></div>
      <Button aria-label="获取上游模型列表" icon={<CloudDownloadOutlined aria-hidden />} loading={busy}
        onClick={() => void refreshUpstreamModels()}>
        获取上游模型列表
      </Button></div>
    {error && <Alert className="notice" showIcon type="error" message={error} />}
    {notice && <Alert className="notice" showIcon type="success" message={notice} />}
    <p className="field-help">“图片理解”开关会立即保存。请为供应商实际支持识图的模型开启，并同时启用模型与供应商。</p>
    <p className="field-help">直接生图需选择供应商支持的图片生成协议，在 LLM 设置中选择对应的图片生成模型，并在 Agent 允许工具中选择“模型直接生图”。</p>
    <div className="panel table-wrap"><table className="data-table"><thead><tr>
      <th>上游模型</th><th>是否启用</th><th>图片理解</th><th>图片生成协议</th>
    </tr></thead><tbody>{page.items.map(item => <tr key={item.id}>
      <td><strong>{item.upstream_model}</strong></td>
      <td><Switch aria-label={'启用 ' + item.upstream_model} checked={item.enabled} disabled={busy}
        checkedChildren="启用" unCheckedChildren="停用" onChange={enabled => void updateModel(item, { enabled })} /></td>
      <td><Switch aria-label={'图片理解 ' + item.upstream_model} checked={item.media?.vision_enabled ?? false}
        disabled={busy || item.media_initialized === false} checkedChildren="开启" unCheckedChildren="关闭"
        onChange={enabled => void toggleVision(item, enabled)} />
        {item.media_initialized === false && <small>媒体数据表未初始化</small>}</td>
      <td><ChoiceSelect aria-label={'图片生成协议 ' + item.upstream_model}
        value={item.media?.image_api_profile ?? 'none'}
        disabled={busy || item.media_initialized === false || provider.protocol === 'ollama'}
        onChange={event => void setImageProfile(item, event as ModelMediaCapabilities['image_api_profile'])}>
        <option value="none">不启用</option><option value="gpt-image">GPT Image</option>
        <option value="legacy-b64">兼容 Base64（PNG）</option>
      </ChoiceSelect></td>
    </tr>)}</tbody></table>{!page.items.length && <p className="empty-list">{busy ? '正在加载模型…' : '暂无已保存模型。可点击“获取上游模型列表”进行同步。'}</p>}</div>
    <div className="catalog-pagination">
      <Pagination current={pageNumber} pageSize={pageSize} total={page.total} disabled={busy}
        showSizeChanger pageSizeOptions={[10, 20, 50, 100]} showTotal={total => `共 ${total} 个模型`}
        onChange={(number, size) => { setPageNumber(number); setPageSize(size); }} />
    </div>
  </div>;
}
