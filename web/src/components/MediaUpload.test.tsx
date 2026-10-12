import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { MediaApi } from '../media-api';
import { MediaUpload } from './MediaUpload';

const asset={id:'a'.repeat(32),purpose:'input' as const,mime_type:'image/png',size_bytes:80,sha256:'b'.repeat(64),status:'ready' as const,created_at:'2026-10-12T00:00:00',expires_at:'2026-10-13T00:00:00'};
it('超过数量和累计大小时不上传',async()=>{
  const api=new MediaApi('offline');const upload=vi.spyOn(api,'upload');
  render(<MediaUpload api={api} sessionId="session" maximumCount={2} onChange={vi.fn()}/>);
  const file=new File(['png'],'a.png',{type:'image/png'});
  fireEvent.change(screen.getByLabelText('添加图片'),{target:{files:[file,file,file]}});
  expect(await screen.findByRole('alert')).toHaveTextContent('最多 2 张');expect(upload).not.toHaveBeenCalled();
});
it('累计超过 20 MiB 时整批拒绝',async()=>{
  const api=new MediaApi('offline'),upload=vi.spyOn(api,'upload');render(<MediaUpload api={api} sessionId="session" maximumCount={4} onChange={vi.fn()}/>);
  const files=Array.from({length:3},()=>{const file=new File(['png'],'a.png',{type:'image/png'});Object.defineProperty(file,'size',{value:8*1024*1024});return file;});
  fireEvent.change(screen.getByLabelText('添加图片'),{target:{files}});expect(await screen.findByRole('alert')).toHaveTextContent('合计不能超过 20 MiB');expect(upload).not.toHaveBeenCalled();
});
it('失败文件可单独重试，用过的文件重传后才能再次发送',async()=>{
  const api=new MediaApi('offline');const upload=vi.spyOn(api,'upload').mockRejectedValueOnce(new Error('失败')).mockResolvedValue(asset);
  const onChange=vi.fn();const props={api,sessionId:'session',maximumCount:4,onChange};
  const view=render(<MediaUpload {...props}/>);
  fireEvent.change(screen.getByLabelText('添加图片'),{target:{files:[new File(['png'],'a.png',{type:'image/png'})]}});
  fireEvent.click(await screen.findByRole('button',{name:'重试 a.png'}));
  await waitFor(()=>expect(onChange).toHaveBeenLastCalledWith([asset]));
  view.rerender(<MediaUpload {...props} consumedIds={[asset.id]}/>);
  await waitFor(()=>expect(onChange).toHaveBeenLastCalledWith([]));
  fireEvent.click(screen.getByRole('button',{name:'重新上传 a.png'}));await waitFor(()=>expect(upload).toHaveBeenCalledTimes(3));
});
it('上传和内容请求携带认证头且不把令牌放入 URL',async()=>{
  const fetcher=vi.fn().mockResolvedValueOnce(Response.json(asset)).mockResolvedValueOnce(new Response('png'));
  vi.stubGlobal('fetch',fetcher);const api=new MediaApi('secret');const file=new File(['png'],'a.png',{type:'image/png'});
  await api.upload(file,'session',new AbortController().signal);await api.content(asset.id,new AbortController().signal);
  expect(fetcher.mock.calls[0][1].headers.Authorization).toBe('Bearer secret');expect(fetcher.mock.calls[0][1].body).toBe(file);
  expect(fetcher.mock.calls[1][1].headers.Authorization).toBe('Bearer secret');expect(fetcher.mock.calls[1][0]).not.toContain('secret');
});
