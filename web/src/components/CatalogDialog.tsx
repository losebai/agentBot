import { Alert, Modal } from 'antd';
import { useEffect, useRef, type ReactNode } from 'react';

/** 目录表单共用弹窗：提交时禁止关闭，保存失败聚焦错误并滚入视口，保留输入草稿。 */
export function CatalogDialog({ title, busy, error, onCancel, children }: {
  title: string; busy: boolean; error?: string; onCancel: () => void; children: ReactNode;
}) {
  const errorRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    // 长表单从底部提交时，焦点移到中文错误，浏览器同步将其滚入弹窗可见区域。
    if (error) errorRef.current?.focus();
  }, [error]);
  return <Modal open centered title={title} width={760} footer={null}
    className="catalog-dialog" maskClosable={false} closable={!busy} keyboard={!busy}
    onCancel={() => { if (!busy) onCancel(); }}
    styles={{ body: { maxHeight: 'calc(100dvh - 180px)', overflowY: 'auto' } }}>
    {error && <div ref={errorRef} role="group" aria-label="操作错误" tabIndex={-1} className="notice">
      <Alert type="error" showIcon message={error} />
    </div>}
    {children}
  </Modal>;
}
