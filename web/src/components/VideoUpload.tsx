import { Alert, Button } from 'antd';
import { useEffect, useRef, useState } from 'react';
import { errorMessage } from '../api';
import type { MediaApi } from '../media-api';
import type { MediaAsset } from '../media-types';

/** 视频只提供给投稿工具，不能进入图像附件或视觉模型请求。 */
export function VideoUpload({ api, sessionId, disabled, onChange, onBusyChange }: {
  api: MediaApi; sessionId: string; disabled: boolean;
  onChange: (asset: MediaAsset | undefined) => void; onBusyChange: (busy: boolean) => void;
}) {
  const [name, setName] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const current = useRef<AbortController>();
  useEffect(() => () => { current.current?.abort(); }, []);

  /** 切换会话或卸载终止读取；已提交后端写入由后端负责安全收尾。 */
  async function upload(file: File) {
    if (file.type !== 'video/mp4' || file.size > 100 * 1024 * 1024 || !file.size) {
      setError('请选择不超过 100 MB 的 MP4 视频。'); return;
    }
    current.current?.abort();
    const controller = new AbortController(); current.current = controller;
    setBusy(true); onBusyChange(true); setError(''); onChange(undefined); setName('');
    try {
      const asset = await api.uploadVideo(file, sessionId, controller.signal);
      if (!controller.signal.aborted) { setName(file.name); onChange(asset); }
    } catch (failure) {
      if (!controller.signal.aborted) setError(errorMessage(failure));
    } finally {
      if (!controller.signal.aborted) { setBusy(false); onBusyChange(false); }
    }
  }

  return <section aria-label="投稿视频">
    <label>添加投稿视频 <input aria-label="添加投稿视频" type="file" accept="video/mp4"
      disabled={disabled || busy} onChange={event => {
        const file = event.target.files?.[0]; event.target.value = '';
        if (file) void upload(file);
      }} /></label>
    {busy && <p>正在上传视频…</p>}
    {name && <p>{name} · 已上传 <Button size="small" disabled={disabled || busy}
      onClick={() => { setName(''); onChange(undefined); }}>移除视频</Button></p>}
    {error && <Alert type="error" message={error} />}
  </section>;
}
