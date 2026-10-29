import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
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

it('同一入口混选图片与视频，分别上传且视频不占图片数量和总大小', async () => {
  const api = new MediaApi('offline');
  const videoAsset = {...asset, id: 'c'.repeat(32), mime_type: 'video/mp4'};
  const image = new File(['png'], '封面.png', {type: 'image/png'});
  const video = new File(['mp4'], '投稿.mp4', {type: 'video/mp4'});
  Object.defineProperty(video, 'size', {value: 80 * 1024 * 1024});
  const imageUpload = vi.spyOn(api, 'upload').mockResolvedValue(asset);
  const videoUpload = vi.spyOn(api, 'uploadVideo').mockResolvedValue(videoAsset);
  const changed = vi.fn(), videoChanged = vi.fn(), busy = vi.fn();
  render(<MediaUpload api={api} sessionId="current" maximumCount={1} allowVideo label="添加文件"
    onChange={changed} onVideoChange={videoChanged} onBusyChange={busy}/>);
  fireEvent.change(screen.getByLabelText('添加文件'), {target: {files: [video, image]}});
  await screen.findByText('封面.png · 已上传');
  expect(imageUpload).toHaveBeenCalledWith(image, 'current', expect.any(AbortSignal));
  expect(videoUpload).toHaveBeenCalledWith(video, 'current', expect.any(AbortSignal));
  expect(changed).toHaveBeenLastCalledWith([asset]);
  expect(videoChanged).toHaveBeenLastCalledWith(videoAsset);
  expect(busy).toHaveBeenLastCalledWith(false);
  fireEvent.click(screen.getByRole('button', {name: '移除 投稿.mp4'}));
  expect(videoChanged).toHaveBeenLastCalledWith(undefined);
  expect(changed).toHaveBeenLastCalledWith([asset]);
});

it('未开放视频时拒绝 MP4，不绕过插件或混入图片接口', async () => {
  const api = new MediaApi('offline');
  const imageUpload = vi.spyOn(api, 'upload'), videoUpload = vi.spyOn(api, 'uploadVideo');
  render(<MediaUpload api={api} sessionId="current" maximumCount={4} label="添加文件" onChange={vi.fn()}/>);
  expect(screen.getByLabelText('添加文件')).not.toHaveAttribute('accept', expect.stringContaining('video/mp4'));
  fireEvent.change(screen.getByLabelText('添加文件'), {target: {files: [new File(['mp4'], 'a.mp4', {type: 'video/mp4'})]}});
  expect(await screen.findByRole('alert')).toHaveTextContent('请选择');
  expect(imageUpload).not.toHaveBeenCalled(); expect(videoUpload).not.toHaveBeenCalled();
});

it.each(['multiple', 'large', 'empty'])('视频 %s 超限时整批拒绝上传', async kind => {
  const api = new MediaApi('offline');
  const imageUpload = vi.spyOn(api, 'upload'), videoUpload = vi.spyOn(api, 'uploadVideo');
  const video = new File(kind === 'empty' ? [] : ['mp4'], 'a.mp4', {type: 'video/mp4'});
  if (kind === 'large') Object.defineProperty(video, 'size', {value: 101 * 1024 * 1024});
  render(<MediaUpload api={api} sessionId="current" maximumCount={4} allowVideo label="添加文件"
    onChange={vi.fn()} onVideoChange={vi.fn()}/>);
  fireEvent.change(screen.getByLabelText('添加文件'), {target: {files: kind === 'multiple' ? [video, video] : [video]}});
  expect(await screen.findByRole('alert')).toHaveTextContent(kind === 'multiple' ? '最多 1 个视频' : '100 MB');
  expect(imageUpload).not.toHaveBeenCalled(); expect(videoUpload).not.toHaveBeenCalled();
});

it('切换会话取消统一队列，晚到视频与尚未开始的图片不能写入新会话', async () => {
  const api = new MediaApi('offline');
  let signal: AbortSignal | undefined, finish: ((value: typeof asset) => void) | undefined;
  vi.spyOn(api, 'uploadVideo').mockImplementation((_file, _session, current) => {
    signal = current;
    return new Promise(resolve => { finish = resolve; });
  });
  const imageUpload = vi.spyOn(api, 'upload');
  const changed = vi.fn(), videoChanged = vi.fn();
  const props = {api, maximumCount: 4, allowVideo: true, label: '添加文件',
    onChange: changed, onVideoChange: videoChanged};
  const view = render(<MediaUpload {...props} sessionId="old"/>);
  fireEvent.change(screen.getByLabelText('添加文件'), {target: {files: [
    new File(['mp4'], '旧视频.mp4', {type: 'video/mp4'}),
    new File(['png'], '旧图片.png', {type: 'image/png'}),
  ]}});
  expect(signal).toBeDefined();
  view.rerender(<MediaUpload {...props} sessionId="new"/>);
  expect(signal?.aborted).toBe(true);
  await act(async () => { finish!({...asset, mime_type: 'video/mp4'}); });
  expect(screen.queryByText(/旧视频|旧图片/)).not.toBeInTheDocument();
  expect(imageUpload).not.toHaveBeenCalled();
  expect(changed).toHaveBeenLastCalledWith([]);
  expect(videoChanged).toHaveBeenLastCalledWith(undefined);
});
