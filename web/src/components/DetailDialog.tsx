import { Alert, Button, Modal, Spin } from 'antd';
import type { ReactNode } from 'react';
import { useEffect, useState } from 'react';

/** 只读详情统一弹窗；加载时仍可关闭，由调用方取消请求并释放附件资源。 */
export function DetailDialog({ title, loading = false, error, onClose, children }: {
  title: string; loading?: boolean; error?: string; onClose: () => void; children: ReactNode;
}) {
  const [trigger] = useState(() => document.activeElement);
  // 详情关闭会立即卸载以取消请求，需主动恢复焦点，支持多层弹窗逐层使用 Esc。
  useEffect(() => () => {
    if (trigger instanceof HTMLElement && trigger.isConnected) trigger.focus({ preventScroll: true });
  }, [trigger]);
  return <Modal open centered title={title} width={880} className="detail-dialog"
    onCancel={onClose} footer={<Button onClick={onClose}>关闭详情</Button>}
    styles={{ body: { maxHeight: 'calc(100dvh - 200px)', overflowY: 'auto' } }}>
    <div className="record-detail" style={{ padding: 0 }}>
      {error && <Alert className="notice" type="error" showIcon message={error} />}
      {loading && <div role="status" aria-label="正在加载详情"><Spin size="small" /> 正在加载详情…</div>}
      {children}
    </div>
  </Modal>;
}
