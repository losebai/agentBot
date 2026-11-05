import { Pagination } from 'antd';

/** 所有管理列表统一使用页码及每页条数，不再展示方向文本按钮。 */
export function RecordPagination({ current, pageSize, total, loading, hasNext, countFailed = false, onChange }: {
  current: number; pageSize: number; total: number | null | undefined; loading: boolean; hasNext: boolean;
  countFailed?: boolean;
  onChange: (page: number, size: number) => void;
}) {
  // 计数失败时只开放已知范围；计数恢复后自动呈现完整页码。
  const actualTotal = typeof total === 'number' ? total : null;
  const knownTotal = actualTotal ?? Math.max(current * pageSize, (current - 1) * pageSize + (hasNext ? pageSize + 1 : 0));

  return <nav className="catalog-pagination record-pagination" aria-label="记录分页">
    <span role="status" aria-label="分页信息">{actualTotal === null
      ? `第 ${current} 页 · ${countFailed ? '总条数暂不可用' : '正在统计总条数…'}`
      : `共 ${actualTotal.toLocaleString('zh-CN')} 条`}</span>
    <Pagination current={current} pageSize={pageSize} total={knownTotal} disabled={loading}
      showSizeChanger showQuickJumper pageSizeOptions={[10, 20, 50, 100]}
      showTotal={value => `共 ${value.toLocaleString('zh-CN')} 条`}
      onChange={(page, size) => onChange(size === pageSize ? page : 1, size)} />
  </nav>;
}
