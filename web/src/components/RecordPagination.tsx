import { Button } from 'antd';
import { useEffect, useState } from 'react';
import { ChoiceSelect } from './ChoiceSelect';

/** 总数独立加载；可直接选择页码或输入跳页，未知总数时仍能继续按页浏览。 */
export function RecordPagination({ current, pageSize, total, loading, hasNext, countFailed = false, onChange }: {
  current: number; pageSize: number; total: number | null; loading: boolean; hasNext: boolean;
  countFailed?: boolean;
  onChange: (page: number, size: number) => void;
}) {
  const [jump, setJump] = useState(String(current));
  useEffect(() => setJump(String(current)), [current]);
  const last = total === null ? null : Math.max(1, Math.ceil(total / pageSize));
  const pages = last === null ? [current] : [...new Set([
    1, ...Array.from({ length: 5 }, (_, index) => current - 2 + index), last,
  ])].filter(page => page >= 1 && page <= last).sort((a, b) => a - b);

  return <nav className="catalog-pagination record-pagination" aria-label="记录分页">
    <span role="status" aria-label="分页信息">{total === null
      ? `第 ${current} 页 · ${countFailed ? '总条数暂不可用' : '正在统计总条数…'}`
      : `共 ${total.toLocaleString('zh-CN')} 条 · 第 ${current} / ${last} 页`}</span>
    <Button disabled={loading || current === 1} onClick={() => onChange(current - 1, pageSize)}>上一页</Button>
    {pages.map((page, index) => <span className="record-page-number" key={page}>
      {index > 0 && page > pages[index - 1] + 1 && <span aria-hidden="true">…</span>}
      <Button type={page === current ? 'primary' : 'default'} aria-label={`第 ${page} 页`}
        aria-current={page === current ? 'page' : undefined} disabled={loading}
        onClick={() => onChange(page, pageSize)}>{page}</Button>
    </span>)}
    <Button disabled={loading || (last === null ? !hasNext : current >= last)}
      onClick={() => onChange(current + 1, pageSize)}>下一页</Button>
    <ChoiceSelect aria-label="每页条数" value={String(pageSize)} disabled={loading}
      onChange={size => onChange(1, Number(size))}>
      {[20, 50, 100].map(size => <option key={size} value={size}>{size} 条/页</option>)}
    </ChoiceSelect>
    <form className="record-page-jump" onSubmit={event => {
      event.preventDefault();
      const page = Number(jump);
      if (Number.isInteger(page) && page >= 1 && last !== null && page <= last) onChange(page, pageSize);
    }}>
      <label>跳至 <input aria-label="跳转页码" type="number" min={1} max={last ?? undefined}
        value={jump} disabled={loading || last === null} onChange={event => setJump(event.target.value)} /> 页</label>
      <Button htmlType="submit" aria-label="跳转" disabled={loading || last === null}>跳转</Button>
    </form>
  </nav>;
}
