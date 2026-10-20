import { Tag } from 'antd';
import type { MemoryEntry, MemoryKind, MemoryScope, MemoryStatus } from '../knowledge-types';

export const memoryScopeNames: Record<MemoryScope, string> = {global: '全局', session: '会话', user: '用户', group: '群聊'};
export const memoryKindNames: Record<MemoryKind, string> = {relationship: '人物关系', preference: '偏好', agreement: '约定', task: '任务', fact: '事实', correction: '更正'};
export const memoryStatusNames: Record<MemoryStatus | 'future', string> = {active: '有效', pending: '待确认', disputed: '有冲突', expired: '已失效', merged: '已合并', deleted: '已遗忘', future: '尚未生效'};
export const memorySourceNames: Record<string, string> = {self_report: '本人自述', user_correction: '用户更正', admin_confirmation: '管理员确认', tool_result: '工具结果', third_party: '第三方转述', legacy_unknown: '来源未知'};
const subjectNames: Record<string, string> = {user: '用户', group: '群聊', shared: '共享事实', unknown: '未知'};

/** 只展示已认证目录返回的标题，不从称谓猜测事实归属。 */
export function memoryScopeLabel(item: {scope: MemoryScope; scope_id: string; scope_title?: string | null}) {
  return memoryScopeNames[item.scope] + ' · ' + (item.scope_title || item.scope_id.slice(0, 12) + '…');
}

/** 保留原始时间与时区；旧数据没有时间时明确显示未知。 */
function timeLabel(value?: string | null) { return value ? value.replace('T', ' ') : '未知'; }

/** 事实与证据分别展示，管理员录入或工具结果不自动等同本人偏好。 */
export function MemoryFacts({memory}: {memory: MemoryEntry}) {
  return <>
    <h3>{memory.key}</h3>
    <p>{memory.tags.map(tag => <Tag key={tag}>{tag}</Tag>)}</p>
    <p>作用域：{memoryScopeLabel(memory)}</p>
    <p>主题：{memory.topic || '未设置'} · 类型：{memoryKindNames[memory.kind] || '未知'} · 版本：{memory.revision ?? '未知'}</p>
    <p>状态：{memoryStatusNames[memory.effective_status || memory.status] || '未知'}</p>
    <p>事实主体：{subjectNames[memory.subject_type] || '未知'}{memory.subject_id ? ` · ${memory.subject_id}` : ''}</p>
    <pre className="knowledge-content">{memory.content}</pre>
    <p>事实时间：{timeLabel(memory.observed_at)}</p>
    <p>生效时间：{memory.valid_from ? timeLabel(memory.valid_from) : '未设置'}</p>
    <p>失效时间：{memory.valid_until ? timeLabel(memory.valid_until) : '未设置'}</p>
    <p>最近使用：{timeLabel(memory.last_used_at)}</p>
    <p>创建时间：{timeLabel(memory.created_at)} · 修改时间：{timeLabel(memory.updated_at)}</p>
    {memory.created_time_basis === 'legacy_unspecified' && <p>创建时间时区未知，保留旧记录原值，不用于判断事实的新旧。</p>}
    {memory.updated_time_basis === 'legacy_unspecified' && <p>修改时间时区未知，保留旧记录原值，不用于判断事实的新旧。</p>}
    {memory.material_ids?.length ? <p>关联素材：{memory.material_ids.map(id => <Tag key={id}>{id}</Tag>)}</p> : null}
    <h4>来源证据</h4>
    {(!memory.source_kind || memory.source_kind === 'legacy_unknown') && <p>来源未知，不能视为本人已确认。</p>}
    {memory.source_kind && memory.source_kind !== 'legacy_unknown' && <p>{memorySourceNames[memory.source_kind] || '未识别来源'}</p>}
    {memory.sources?.length ? <ul>{memory.sources.map(source => <li key={source.id}>
      <strong>{memorySourceNames[source.source_kind] || '未识别来源'}</strong>
      <p>来源时间：{timeLabel(source.source_time)}</p>
      {source.source_actor_id && <p>来源主体：{source.source_actor_id}</p>}
      {source.source_ref && <p>来源引用：{source.source_ref}</p>}
      {source.recorded_at && <p>记录时间：{timeLabel(source.recorded_at)}</p>}
      {source.excerpt && <blockquote>{source.excerpt}</blockquote>}
    </li>)}</ul> : <p>没有可核验的原始片段。</p>}
  </>;
}
