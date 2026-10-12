import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { MediaGenerator } from './MediaGenerator';

it('仅列出具备能力的模型，切模式取消正在生成的请求',async()=>{
  let generation:AbortSignal|undefined;
  vi.stubGlobal('fetch',vi.fn(async(url,options)=>{
    if(String(url).includes('ai/models'))return Response.json({items:[{id:1,alias:'image',enabled:true,media_initialized:true,media:{image_api_profile:'gpt-image'}},{id:2,alias:'plain',enabled:true,media_initialized:true,media:{image_api_profile:'none'}}],next_cursor:null});
    if(String(url).endsWith('/status'))return Response.json({media_backend:'python',python_media:{initialized:true,storage_available:true}});
    generation=options.signal;return new Promise(()=>{});
  }));
  render(<MediaGenerator token="offline"/>);
  expect(await screen.findByRole('option',{name:'image'})).toBeVisible();expect(screen.queryByRole('option',{name:'plain'})).toBeNull();
  fireEvent.change(screen.getByLabelText('生成模型'),{target:{value:'image'}});fireEvent.change(screen.getByLabelText('生成描述'),{target:{value:'猫'}});
  fireEvent.click(screen.getByRole('button',{name:'开始生成'}));await waitFor(()=>expect(generation).toBeDefined());
  fireEvent.change(screen.getByLabelText('生成类型'),{target:{value:'AUDIO'}});expect(generation!.aborted).toBe(true);
});
