import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import type { Provider } from '../ai-types';
import { ProviderModels } from './ProviderModels';

const provider=(id:number):Provider=>({id,revision:1,name:`供应商${id}`,protocol:'openai-chat',base_url:`https://provider${id}.invalid/v1`,enabled:true,description:'',credential_set:true,credential_available:true});

it('切换供应商取消旧请求并忽略延迟返回的旧模型列表',async()=>{
  let oldResult:(value:Response)=>void=()=>{};
  let oldSignal:AbortSignal|undefined;
  const fetcher=vi.fn(async(url,options)=>{
    if(String(url).includes('providers/1/upstream-models')) {
      oldSignal=options?.signal;
      return new Promise<Response>(resolve=>{oldResult=resolve;});
    }
    if(String(url).includes('upstream-models'))return Response.json({provider_id:2,revision:1,models:['provider-two-model']});
    return Response.json({items:[],next_cursor:null});
  });
  vi.stubGlobal('fetch',fetcher);
  const view=render(<ProviderModels token="offline" provider={provider(1)}/>);
  await waitFor(()=>expect(oldSignal).toBeDefined());
  view.rerender(<ProviderModels token="offline" provider={provider(2)}/>);
  expect(await screen.findByText('provider-two-model')).toBeVisible();
  expect(oldSignal!.aborted).toBe(true);
  await act(async()=>oldResult(Response.json({provider_id:1,revision:1,models:['old-provider-model']})));
  expect(screen.queryByText('old-provider-model')).not.toBeInTheDocument();
  expect(screen.getByText('provider-two-model')).toBeVisible();
});

it('获取失败显示错误，刷新仅重试所选供应商且不发生成请求',async()=>{
  let count=0;
  const fetcher=vi.fn(async(url)=>{
    if(String(url).includes('upstream-models')) {
      count+=1;
      return count===1?Response.json({code:'AI_UPSTREAM_AUTH_REJECTED',message:'上游拒绝鉴权'},{status:502}):Response.json({provider_id:1,revision:1,models:[]});
    }
    return Response.json({items:[],next_cursor:null});
  });
  vi.stubGlobal('fetch',fetcher);render(<ProviderModels token="offline" provider={provider(1)}/>);
  expect(await screen.findByText('获取或导入模型失败：上游拒绝鉴权')).toBeVisible();
  fireEvent.click(screen.getByRole('button',{name:'刷新模型'}));
  expect(await screen.findByText('没有匹配的上游模型。')).toBeVisible();
  expect(fetcher.mock.calls.every(([url])=>!String(url).includes('chat'))).toBe(true);
});
