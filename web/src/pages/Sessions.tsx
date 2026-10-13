import { useState } from 'react';
import { CallRecords } from './CallRecords';
import { MediaRuns } from './MediaRuns';

/** 普通和 Agent 共用调用列表，媒体保留文件管理入口。 */
export function Sessions({token}: {token: string}) {
  const [mode, setMode] = useState('calls');
  return <><div className="mode-tabs" role="tablist" aria-label="记录类型">
    <button role="tab" aria-selected={mode === 'calls'} onClick={() => setMode('calls')}>调用记录</button>
    <button role="tab" aria-selected={mode === 'media'} onClick={() => setMode('media')}>媒体运行记录</button>
  </div>{mode === 'media' ? <MediaRuns token={token} /> : <CallRecords token={token} />}</>;
}
