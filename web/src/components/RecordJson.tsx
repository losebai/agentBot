import { useMemo, useState } from 'react';

/** 长 JSON 仅在用户展开时格式化和挂载，避免打开详情就构建数万字的 DOM。 */
export function RecordJson({ title, value, empty }: { title: string; value: unknown; empty: string }) {
  const [open, setOpen] = useState(false);
  const content = useMemo(() => open ? value == null ? empty : JSON.stringify(value, null, 2) : '',
    [open, value, empty]);
  return <details className="record-json" open={open}>
    <summary onClick={event => { event.preventDefault(); setOpen(value => !value); }}>{title}</summary>
    {open && <pre>{content}</pre>}
  </details>;
}
