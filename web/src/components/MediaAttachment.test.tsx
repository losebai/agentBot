import { render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { MediaApi } from '../media-api';
import { ApiFailure } from '../api';
import { MediaAttachment } from './MediaAttachment';

export const asset = {id:'a'.repeat(32),purpose:'output' as const,mime_type:'image/png',size_bytes:80,sha256:'b'.repeat(64),status:'ready' as const,created_at:'2026-10-12T00:00:00',expires_at:'2026-10-13T00:00:00'};
it('认证视频预览提供控件和下载，卸载后释放 URL', async()=>{
  const api=new MediaApi('offline');vi.spyOn(api,'content').mockResolvedValue(new Blob(['mp4'],{type:'video/mp4'}));
  URL.createObjectURL=vi.fn(()=> 'blob:video');URL.revokeObjectURL=vi.fn();
  const view=render(<MediaAttachment api={api} asset={{...asset,mime_type:'video/mp4'}}/>);
  expect(await screen.findByRole('link',{name:'下载附件'})).toHaveAttribute('download',`${asset.id}.mp4`);
  const video=view.container.querySelector('video');
  expect(video).toHaveAttribute('src','blob:video');
  expect(video).toHaveAttribute('controls');
  expect(view.container.querySelector('audio')).toBeNull();
  view.unmount();expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:video');
});
it('认证预览在卸载后撤销 Blob URL', async()=>{
  const api=new MediaApi('offline');vi.spyOn(api,'content').mockResolvedValue(new Blob(['png']));
  URL.createObjectURL=vi.fn(()=> 'blob:preview');URL.revokeObjectURL=vi.fn();
  const view=render(<MediaAttachment api={api} asset={asset}/>);
  expect(await screen.findByRole('img')).toHaveAttribute('src','blob:preview');
  view.unmount();expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:preview');
});
it('卸载后才完成的内容请求不留下 Blob URL',async()=>{
  const api=new MediaApi('offline');let resolve!:(b:Blob)=>void;
  vi.spyOn(api,'content').mockImplementation(()=>new Promise(r=>{resolve=r;}));
  URL.createObjectURL=vi.fn(()=> 'blob:late');URL.revokeObjectURL=vi.fn();
  const view=render(<MediaAttachment api={api} asset={asset}/>);view.unmount();resolve(new Blob(['png']));
  await waitFor(()=>expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:late'));
});
it('过期显示清晰状态，PCM 只提供下载',async()=>{
  const api=new MediaApi('offline');vi.spyOn(api,'content').mockRejectedValue(new ApiFailure('gone',410,'MEDIA_EXPIRED'));
  const view=render(<MediaAttachment api={api} asset={asset}/>);expect(await screen.findByText('附件已过期')).toBeVisible();view.unmount();
  vi.spyOn(api,'content').mockResolvedValue(new Blob(['pcm']));URL.createObjectURL=vi.fn(()=> 'blob:pcm');
  render(<MediaAttachment api={api} asset={{...asset,mime_type:'audio/pcm'}}/>);
  expect(await screen.findByRole('link',{name:'下载附件'})).toHaveAttribute('href','blob:pcm');
  expect(screen.getByText(/PCM 需要支持原始音频/)).toBeVisible();expect(document.querySelector('audio')).toBeNull();
});
