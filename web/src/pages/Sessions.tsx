import { useState } from 'react';
import { CallRecords } from './CallRecords';
import { MediaRuns } from './MediaRuns';

/** 普通调用、系统调用和媒体分开查看；切换标签重新建立筛选与分页状态。 */
export function Sessions({token}: {token: string}) {
  const [mode, setMode] = useState('calls');
  return <><div className="mode-tabs" role="tablist" aria-label="记录类型">
    <button role="tab" aria-selected={mode === 'calls'} onClick={() => setMode('calls')}>调用记录</button>
    <button role="tab" aria-selected={mode === 'system'} onClick={() => setMode('system')}>系统调用</button>
    <button role="tab" aria-selected={mode === 'media'} onClick={() => setMode('media')}>媒体运行记录</button>
  </div>{mode === 'media' ? <MediaRuns token={token} /> : <CallRecords key={mode} token={token} category={mode === 'system' ? 'system' : 'conversation'} />}</>;
}
