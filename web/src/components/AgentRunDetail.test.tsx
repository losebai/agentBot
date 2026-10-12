import { render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { AgentApi } from '../agent-api';
import { AgentRunDetail } from './AgentRunDetail';

it('查看记录不重新执行 Agent，running 保持原状态',async()=>{
 const fetcher=vi.fn(async (url)=>Response.json(String(url).includes('/steps')?{items:[{sequence:1,turn:1,tool:'lookup',status:'unknown',summary:'AGENT_TOOL_OUTCOME_UNKNOWN'}],next_cursor:null}:{execution_id:'a'.repeat(32),status:'running',prompt:'输入',content:'部分内容',revisions:{}}));
 vi.stubGlobal('fetch',fetcher);render(<AgentRunDetail api={new AgentApi('offline')} id={'a'.repeat(32)} onClose={()=>{}}/>);
 expect(await screen.findByText('运行中')).toBeInTheDocument();
 expect(await screen.findByText(/AGENT_TOOL_OUTCOME_UNKNOWN/)).toBeInTheDocument();
 expect(fetcher.mock.calls).toHaveLength(2);
});
it('详情关闭后取消未完成的附件请求并撤销迟到 Blob',async()=>{
 let resolve!:(v:Response)=>void,signal:AbortSignal|undefined;
 const asset={id:'b'.repeat(32),purpose:'output',mime_type:'image/png',size_bytes:80,sha256:'c'.repeat(64),status:'ready',created_at:'2026-10-12T00:00:00',expires_at:'2026-10-13T00:00:00'};
 URL.createObjectURL=vi.fn(()=> 'blob:late-detail');URL.revokeObjectURL=vi.fn();
 vi.stubGlobal('fetch',vi.fn(async(url,init)=>{
  if(String(url).endsWith('/content')){signal=init.signal;return new Promise<Response>(r=>{resolve=r;});}
  return Response.json(String(url).includes('/steps')?{items:[],next_cursor:null}:{execution_id:'a'.repeat(32),status:'cancelled',prompt:'生成',content:'',attachments:[asset]});
 }));const view=render(<AgentRunDetail api={new AgentApi('offline')} id={'a'.repeat(32)} onClose={()=>{}}/>);await waitFor(()=>expect(signal).toBeDefined());view.unmount();expect(signal!.aborted).toBe(true);resolve(new Response('image'));await waitFor(()=>expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:late-detail'));
});
