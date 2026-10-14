import { KnowledgeRecords } from './KnowledgeRecords';

/** 共享知识采用只新增表单，不包含用户或群聊归属字段。 */
export function KnowledgeEntries({token}: {token: string}) {
  return <KnowledgeRecords token={token} kind="knowledge" />;
}
