import { Button } from 'antd';
import { useEffect, useState } from 'react';
import { errorMessage } from '../api';
import type { KnowledgeApi } from '../knowledge-api';
import type { CatalogPage } from '../ai-types';
import type { MemoryEntry, MemoryRevision } from '../knowledge-types';
import { DetailDialog } from './DetailDialog';
import { MemoryFacts } from './MemoryFacts';
import { MemoryConflicts } from './MemoryConflicts';
import { RecordPagination } from './RecordPagination';

const operationNames: Record<string, string> = {create: '新增', revise: '修订', confirm: '确认来源', dispute: '标记冲突', resolve: '解决冲突', expire: '失效', merge: '合并', forget: '遗忘', migrate: '历史迁入'};

/** 当前事实与历史独立读取，关闭或切换版本分页时取消旧请求。 */
export function MemoryDetail({api, id, onClose, onChanged}: {api: KnowledgeApi; id: number; onClose: () => void; onChanged: () => void}) {
  const [memory, setMemory] = useState<MemoryEntry>();
  const [loading, setLoading] = useState(true), [error, setError] = useState('');
  const [historyOpen, setHistoryOpen] = useState(false), [pageNumber,setPageNumber]=useState(1), [pageSize,setPageSize]=useState(10);
  const [history, setHistory] = useState<CatalogPage<MemoryRevision>>({items: [], next_cursor: null, total: 0});
  const [historyLoading, setHistoryLoading] = useState(false), [historyError, setHistoryError] = useState('');
  const [conflictsOpen, setConflictsOpen] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    /** 详情必须读取完整正文，列表文本不可用于核实来源。 */
    async function load() {
      try {
        const value = await api.getMemory(id, controller.signal);
        if (!controller.signal.aborted) setMemory(value);
      } catch (failure) { if (!controller.signal.aborted) setError(errorMessage(failure)); }
      finally { if (!controller.signal.aborted) setLoading(false); }
    }
    void load();
    return () => controller.abort();
  }, [api, id]);
  useEffect(() => {
    if (!historyOpen) return;
    const controller = new AbortController();
    setHistoryLoading(true); setHistoryError('');
    /** 历史只读，不提供把旧版本重新写回当前事实的快捷入口。 */
    async function loadHistory() {
      try {
        const value = await api.memoryRevisions(id, pageNumber, pageSize, controller.signal);
        if (!controller.signal.aborted) {setHistory(value);const last=Math.max(1,Math.ceil(value.total/pageSize));if(pageNumber>last)setPageNumber(last);}
      } catch (failure) { if (!controller.signal.aborted) setHistoryError(errorMessage(failure)); }
      finally { if (!controller.signal.aborted) setHistoryLoading(false); }
    }
    void loadHistory();
    return () => controller.abort();
  }, [api, id, historyOpen, pageNumber, pageSize]);
  return <DetailDialog title={`记忆详情 #${id}`} loading={loading} error={error} onClose={onClose}>
    {memory && <MemoryFacts memory={memory} />}
    {/* 遗忘的普通详情会返回 404，但管理员仍应能独立进入授权历史审计。 */}
    {!loading && <Button onClick={() => setHistoryOpen(value => !value)}>{historyOpen ? '收起历史' : '版本历史'}</Button>}
    {memory?.conflict_set_id && ['pending', 'disputed'].includes(memory.status) && <>
      <Button onClick={() => setConflictsOpen(value => !value)}>{conflictsOpen ? '收起冲突' : '查看冲突'}</Button>
      {conflictsOpen && <MemoryConflicts api={api} id={id} onResolved={onChanged} />}
    </>}
    {historyOpen && <section aria-label="版本历史">
      {historyError && <p role="alert">{historyError}</p>}
      {historyLoading ? <p role="status">正在加载版本…</p> : <>
        {history.items.map(revision => <article key={revision.id}>
          <h4>第 {revision.revision} 版 · {operationNames[revision.operation] || revision.operation}</h4>
          <p>{revision.created_at.replace('T', ' ')}</p><p>{revision.reason}</p>
          <MemoryFacts memory={revision.snapshot} />
        </article>)}
        {!history.items.length && <p>暂无版本记录。</p>}
        <RecordPagination current={pageNumber} pageSize={pageSize} total={history.total} loading={historyLoading}
          hasNext={Boolean(history.next_cursor)} onChange={(number,size)=>{setPageNumber(number);setPageSize(size);}}/>
      </>}
    </section>}
  </DetailDialog>;
}
