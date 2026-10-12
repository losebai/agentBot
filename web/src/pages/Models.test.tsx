import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { Models } from './Models';

it('模型别名与完整上游 ID 分开编辑', async () => {
  vi.stubGlobal('fetch', vi.fn(async (url) => Response.json({items:String(url).includes('providers') ?
    [{id:1,name:'本机',enabled:true}] : [{id:1,revision:1,alias:'local-qwen',upstream_model:'Qwen/Qwen3.5-9B',provider_id:1,enabled:false}]})));
  render(<Models token="offline" />);
  fireEvent.click(await screen.findByRole('button', {name:'编辑local-qwen'}));
  expect(screen.getByLabelText('模型别名')).toHaveValue('local-qwen');
  expect(screen.getByLabelText('上游模型 ID')).toHaveValue('Qwen/Qwen3.5-9B');
});

it('能力与普通字段共享版本，冲突保留编辑草稿',async()=>{
  let patch:any;
  vi.stubGlobal('fetch',vi.fn(async(url,options)=>{
    if(options?.method==='PATCH'){patch=JSON.parse(options.body);return Response.json({message:'版本冲突',code:'CONFLICT'},{status:409});}
    return Response.json({items:String(url).includes('providers')?[{id:1,name:'本机',enabled:true,protocol:'openai-chat'}]:[{id:1,revision:3,alias:'vision',upstream_model:'model',provider_id:1,enabled:true,media_initialized:true,media:{vision_enabled:false,image_api_profile:'none',audio_chat_enabled:false}}]});
  }));
  render(<Models token="offline"/>);fireEvent.click(await screen.findByRole('button',{name:'编辑vision'}));
  fireEvent.click(screen.getByLabelText('支持图片理解'));fireEvent.click(screen.getByRole('button',{name:'保存模型'}));
  await waitFor(()=>expect(patch.expected_revision).toBe(3));expect(patch.media.vision_enabled).toBe(true);
  expect(await screen.findByText('版本冲突')).toBeVisible();expect(screen.getByLabelText('支持图片理解')).toBeChecked();
});
