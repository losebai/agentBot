import { KnowledgeRecords } from './KnowledgeRecords';

/** 记忆管理明确选择已有匿名作用域或新的 QQ 目标。 */
export function TaggedMemories({token}: {token: string}) {
  return <KnowledgeRecords token={token} kind="memory" />;
}
