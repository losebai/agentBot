import { useState } from 'react';
import { KnowledgeEntries } from '../components/KnowledgeEntries';
import { MaterialLibrary } from '../components/MaterialLibrary';
import { TaggedMemories } from '../components/TaggedMemories';

/** 切换页签卸载旧列表，取消过时请求并清理详情。 */
export function Knowledge({token}: {token: string}) {
  const [kind, setKind] = useState<'knowledge' | 'memory' | 'material'>('knowledge');
  return <><div className="mode-tabs" role="tablist" aria-label="知识、记忆与素材类型">
    <button role="tab" aria-selected={kind === 'knowledge'} onClick={() => setKind('knowledge')}>知识条目</button>
    <button role="tab" aria-selected={kind === 'memory'} onClick={() => setKind('memory')}>记忆</button>
    <button role="tab" aria-selected={kind === 'material'} onClick={() => setKind('material')}>素材库</button>
  </div>{kind === 'knowledge' ? <KnowledgeEntries token={token} /> : kind === 'memory' ? <TaggedMemories token={token} /> : <MaterialLibrary token={token} />}</>;
}
