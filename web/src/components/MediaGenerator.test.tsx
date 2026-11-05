import { chooseSelect } from '../test-select';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { MediaGenerator } from './MediaGenerator';

it('仅列出具备能力的模型，切模式取消正在生成的请求',async()=>{
  let generation:AbortSignal|undefined;
  vi.stubGlobal('fetch',vi.fn(async(url,options)=>{
    if(String(url).includes('ai/models'))return Response.json({items:[{provider_name: '测试供应商', reference: `model:${1}`, provider_id: 1, upstream_model: 'image', revision: 1, id:1,alias:'image',enabled:true,media_initialized:true,media:{image_api_profile:'gpt-image'}},{provider_name: '测试供应商', reference: `model:${2}`, provider_id: 1, upstream_model: 'plain', revision: 1, id:2,alias:'plain',enabled:true,media_initialized:true,media:{image_api_profile:'none'}}],next_cursor:null});
    if(String(url).endsWith('/status'))return Response.json({media_backend:'python',python_media:{initialized:true,storage_available:true}});
    generation=options.signal;return new Promise(()=>{});
  }));
  render(<MediaGenerator token="offline"/>);
  await chooseSelect(screen.getByRole('combobox', { name: '生成模型' }), 'image');
  expect(screen.queryByRole('option',{name:'plain'})).toBeNull();
  fireEvent.change(screen.getByLabelText('生成描述'),{target:{value:'猫'}});
  fireEvent.click(screen.getByRole('button',{name:'开始生成'}));await waitFor(()=>expect(generation).toBeDefined());
  await chooseSelect(screen.getByRole('combobox', { name: '生成类型' }), '对话音频');expect(generation!.aborted).toBe(true);
});
