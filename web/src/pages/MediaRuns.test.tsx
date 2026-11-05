import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { MediaRuns } from './MediaRuns';

it('运行详情的过期附件不影响正文或其他记录，查看不会重新生成',async()=>{
 const id='a'.repeat(32),asset={id:'b'.repeat(32),purpose:'output',mime_type:'image/png',size_bytes:80,sha256:'c'.repeat(64),status:'expired',created_at:'2026-10-12T00:00:00',expires_at:'2026-10-13T00:00:00'};
 const run={execution_id:id,session_id:'admin_test',model:'model:1',model_display_name:'绘画供应商 / image',output_type:'IMAGE',status:'completed',created_at:'2026-10-12T00:00:00',duration_ms:20};
 const fetcher=vi.fn(async(url)=>Response.json(String(url).endsWith(id)?{...run,prompt:'绘画',content:'已经完成',attachments:[asset]}:{items:[run],next_cursor:null}));vi.stubGlobal('fetch',fetcher);
 render(<MediaRuns token="offline"/>);expect(await screen.findByText('绘画供应商 / image')).toBeVisible();fireEvent.click(screen.getByRole('button',{name:'查看详情'}));await waitFor(()=>expect(screen.getByText('附件已过期')).toBeVisible());expect(screen.getByText('模型：绘画供应商 / image')).toBeVisible();expect(screen.getByText('已经完成')).toBeVisible();expect(fetcher.mock.calls).toHaveLength(2);
});
