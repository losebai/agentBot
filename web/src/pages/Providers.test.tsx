import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { Providers } from './Providers';

it('供应商冲突后保留编辑值且不回填数据库密钥', async () => {
  vi.stubGlobal('fetch', vi.fn(async (_url, options) => options?.method === 'PATCH'
    ? Response.json({code: 'AI_CONFIG_CONFLICT', message: '配置冲突'}, {status:409})
    : Response.json({items:[{id:1,revision:1,name:'本机',protocol:'ollama',base_url:'http://localhost:11434',
      credential_set:true,credential_available:true,enabled:false,description:''}],next_cursor:null})));
  render(<Providers token="offline" />);
  fireEvent.click(await screen.findByRole('button', {name:'编辑本机'}));
  fireEvent.change(screen.getByLabelText('供应商名称'), {target:{value:'修改后的供应商'}});
  fireEvent.click(screen.getByRole('button', {name:'保存供应商'}));
  expect(await screen.findByText('配置冲突')).toBeInTheDocument();
  expect(screen.getByLabelText('供应商名称')).toHaveValue('修改后的供应商');
  expect(screen.getByLabelText('API 密钥')).toHaveValue('');
});

it('保存独立 URL 和 Key 后自动获取供应商模型并可导入', async () => {
  const provider={id:2,revision:1,name:'服务乙',protocol:'openai-chat',base_url:'https://two.invalid/v1',credential_set:true,credential_available:true,enabled:true,description:''};
  const fetcher=vi.fn(async(url, options)=>{
    if(String(url).includes('upstream-models')) return Response.json({provider_id:2,revision:1,models:['same-model']});
    if(String(url).includes('models/import')) return Response.json({provider_id:2,revision:1,imported:[{alias:'2/same-model'}],skipped:[]});
    if(options?.method==='POST') return Response.json(provider,{status:201});
    return Response.json({items:[],next_cursor:null});
  });
  vi.stubGlobal('fetch',fetcher);render(<Providers token="offline"/>);
  await waitFor(()=>expect(screen.getByRole('button',{name:'新增供应商'})).toBeEnabled());
  fireEvent.click(screen.getByRole('button',{name:'新增供应商'}));
  fireEvent.change(screen.getByLabelText('供应商名称'),{target:{value:'服务乙'}});
  fireEvent.change(screen.getByLabelText('API 根地址'),{target:{value:'https://two.invalid/v1'}});
  fireEvent.change(screen.getByLabelText('API 密钥'),{target:{value:'second-key'}});
  fireEvent.click(screen.getByRole('button',{name:'保存供应商'}));
  expect(await screen.findByText('same-model')).toBeVisible();
  const request=fetcher.mock.calls.find(([,options])=>options?.method==='POST')![1]!;
  expect(JSON.parse(request.body).credential).toBe('second-key');
  fireEvent.click(screen.getByLabelText('选择 same-model'));
  fireEvent.click(screen.getByRole('button',{name:'导入所选模型'}));
  await waitFor(()=>expect(fetcher.mock.calls.some(([url])=>String(url).includes('models/import'))).toBe(true));
});
