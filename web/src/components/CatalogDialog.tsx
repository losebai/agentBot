import { Alert, Modal } from 'antd';
import type { ReactNode } from 'react';

/** 目录表单共用弹窗：提交时禁止关闭，错误留在输入附近，长表单独立滚动。 */
export function CatalogDialog({ title, busy, error, onCancel, children }: {
  title: string; busy: boolean; error?: string; onCancel: () => void; children: ReactNode;
}) {
  return <Modal open centered title={title} width={760} footer={null}
    className="catalog-dialog" maskClosable={false} closable={!busy} keyboard={!busy}
    onCancel={() => { if (!busy) onCancel(); }}
    styles={{ body: { maxHeight: 'calc(100dvh - 180px)', overflowY: 'auto' } }}>
    {error && <Alert className="notice" type="error" showIcon message={error} />}
    {children}
  </Modal>;
}
