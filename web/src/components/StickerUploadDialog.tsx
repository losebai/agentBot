import { Button, Modal, Tag } from 'antd';
import { useEffect, useRef, useState } from 'react';
import type { MouseEvent as ReactMouseEvent } from 'react';
import { errorMessage } from '../api';
import type { StickerApi } from '../sticker-api';

type UploadItem = { id: number; file: File; name: string;
  status: 'pending' | 'uploading' | 'saved' | 'failed'; message: string };

const labels = { pending: '待上传', uploading: '上传中', saved: '已保存', failed: '上传失败' };

/** 添加弹窗独立持有本批队列；成功项不会在下一次点击或失败重试时重复提交。 */
export function StickerUploadDialog({ api, onClose, onUploaded }: {
  api: StickerApi; onClose: () => void; onUploaded: () => void;
}) {
  const [items, setItems] = useState<UploadItem[]>([]);
  const [tags, setTags] = useState(''), [autoAnalyze, setAutoAnalyze] = useState(true);
  const [busy, setBusy] = useState(false), [over, setOver] = useState(false);
  const [position, setPosition] = useState({ x: 0, y: 0 });
  const sequence = useRef(0), pending = useRef<AbortController>();
  const moving = useRef<{ x: number; y: number; left: number; top: number;
    minX: number; maxX: number; minY: number; maxY: number }>();
  const panel = useRef<HTMLDivElement>(null), mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    /** 只拖动标题栏；边界保留标题可见，输入框和文件拖放不移动弹窗。 */
    const move = (event: MouseEvent) => {
      const drag = moving.current;
      if (!drag) return;
      setPosition({ x: Math.min(drag.maxX, Math.max(drag.minX, drag.left + event.clientX - drag.x)),
        y: Math.min(drag.maxY, Math.max(drag.minY, drag.top + event.clientY - drag.y)) });
    };
    const stop = () => { moving.current = undefined; };
    window.addEventListener('mousemove', move); window.addEventListener('mouseup', stop);
    // 关闭或切换页面时中断当前HTTP等待，循环不再提交后面的文件。
    return () => {
      mounted.current = false; pending.current?.abort(); moving.current = undefined;
      window.removeEventListener('mousemove', move); window.removeEventListener('mouseup', stop);
    };
  }, []);

  /** 文件名只是显示草稿，不参与磁盘路径；允许不同文件具有相同名称。 */
  function add(files: File[]) {
    if (pending.current) return;
    setItems(previous => [...previous, ...files.map(file => ({
      id: sequence.current++, file, name: file.name, status: 'pending' as const, message: '',
    }))]);
  }

  /** 标题拖动按视口限制位置，正文滚动及选择文字不触发拖拽。 */
  function startMove(event: ReactMouseEvent) {
    if (event.button !== 0 || !panel.current) return;
    event.preventDefault();
    const bounds = panel.current.getBoundingClientRect();
    moving.current = { x: event.clientX, y: event.clientY, left: position.x, top: position.y,
      minX: position.x - bounds.left, maxX: position.x + window.innerWidth - bounds.right,
      minY: position.y - bounds.top, maxY: position.y + window.innerHeight - bounds.top - 48 };
  }

  /** 识图失败与上传失败区分：已保存原件只提示重识图，不重新上传。 */
  async function upload(retry: boolean) {
    if (pending.current) return;
    const selected = items.filter(item => item.status === (retry ? 'failed' : 'pending'));
    if (!selected.length || selected.some(item => !item.name.trim())) return;
    const controller = new AbortController(); pending.current = controller;
    const batchTags = [...new Set(tags.split(/[,，\n]/).map(tag => tag.trim()).filter(Boolean))];
    setBusy(true);
    let saved = 0;
    /** 每个文件只更新自己的状态，慢响应不能把其他队列项还原成旧草稿。 */
    const update = (id: number, values: Partial<UploadItem>) => {
      if (mounted.current && !controller.signal.aborted) {
        setItems(previous => previous.map(item => item.id === id ? { ...item, ...values } : item));
      }
    };
    try {
      // 串行提交匹配现有上传/识图容量，单项失败继续处理本批其余文件。
      for (const item of selected) {
        if (controller.signal.aborted) break;
        update(item.id, { status: 'uploading', message: '' });
        try {
          const result = await api.upload(item.file, item.name.trim(), batchTags,
            controller.signal, autoAnalyze);
          if (controller.signal.aborted) break;
          saved += 1;
          update(item.id, { status: 'saved', message: result.analysis_status === 'failed'
            ? `图片已保存，识图失败：${result.analysis_error || '请重识图'}`
            : result.analysis_status === 'pending' ? '图片已保存，尚未识图。' : '上传和识图完成。' });
        } catch (failure) {
          if (controller.signal.aborted) break;
          update(item.id, { status: 'failed', message: errorMessage(failure) });
        }
      }
    } finally {
      pending.current = undefined;
      if (mounted.current) {
        setBusy(false);
        if (saved) onUploaded();
      }
    }
  }

  const waiting = items.filter(item => item.status === 'pending');
  const failed = items.filter(item => item.status === 'failed');
  return <Modal open width={800} maskClosable={false} onCancel={onClose}
    title={<span className="sticker-upload-title" onMouseDown={startMove}>添加表情包</span>}
    modalRender={node => <div ref={panel} className="sticker-upload-position"
      style={{ transform: `translate(${position.x}px, ${position.y}px)` }}>{node}</div>}
    footer={<>
      <Button onClick={onClose}>{busy ? '停止并关闭' : '关闭'}</Button>
      {!!failed.length && <Button disabled={busy || failed.some(item => !item.name.trim())}
        onClick={() => void upload(true)}>重试失败项</Button>}
      <Button type="primary" loading={busy} disabled={!waiting.length || waiting.some(item => !item.name.trim())}
        onClick={() => void upload(false)}>开始上传</Button>
    </>}>
    <div className={`sticker-dropzone${over ? ' is-over' : ''}`} aria-label="拖拽上传区域"
      onDragOver={event => { event.preventDefault(); if (!busy) setOver(true); }}
      onDragLeave={() => setOver(false)} onDrop={event => {
        event.preventDefault(); setOver(false);
        if (!busy) add(Array.from(event.dataTransfer.files));
      }}>
      <strong>将图片拖到这里，或选择多张图片</strong>
      <label>图片文件<input type="file" multiple disabled={busy}
        accept="image/png,image/jpeg,image/webp,image/gif" onChange={event => {
          add(Array.from(event.target.files || [])); event.target.value = '';
        }} /></label>
      <small>支持 PNG、JPEG、WEBP、GIF，每张最多 10MiB，按实际格式识别并保留动画。</small>
    </div>
    <fieldset className="sticker-form" disabled={busy}>
      <label>上传标签<textarea value={tags} rows={2} placeholder="本批通用标签，用逗号或换行分隔"
        onChange={event => setTags(event.target.value)} /></label>
      <label className="sticker-enabled"><input type="checkbox" checked={autoAnalyze}
        onChange={event => setAutoAnalyze(event.target.checked)} />上传后自动识图</label>
      <small>关闭后仅保存原件和标签；之后可在列表中重识图。开启时使用配置页选定的视觉模型。</small>
      <div className="sticker-upload-queue" aria-live="polite">
        {items.map(item => <div className="sticker-upload-item" key={item.id}>
          <label>{items.length === 1 ? '上传名称' : `名称 ${item.file.name}`}
            <input value={item.name} maxLength={255} disabled={item.status === 'saved'}
              onChange={event => setItems(previous => previous.map(row => row.id === item.id
                ? { ...row, name: event.target.value } : row))} /></label>
          <span>{item.file.name} · {(item.file.size / 1024).toFixed(1)} KiB</span>
          <Tag color={item.status === 'failed' ? 'red' : item.status === 'saved' ? 'green' : undefined}>
            {labels[item.status]}</Tag>
          {item.message && <p>{item.message}</p>}
          {item.status !== 'saved' && <Button size="small" disabled={busy}
            onClick={() => setItems(previous => previous.filter(row => row.id !== item.id))}>移除</Button>}
        </div>)}
      </div>
    </fieldset>
    <p>已保存 {items.filter(item => item.status === 'saved').length} / {items.length} 张，失败 {failed.length} 张。</p>
  </Modal>;
}
