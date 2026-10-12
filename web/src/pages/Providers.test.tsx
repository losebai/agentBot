import { fireEvent, render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { Providers } from './Providers';

it('供应商冲突后保留编辑值且只展示凭据引用', async () => {
  vi.stubGlobal('fetch', vi.fn(async (_url, options) => options?.method === 'PATCH'
    ? Response.json({code: 'AI_CONFIG_CONFLICT', message: '配置冲突'}, {status:409})
    : Response.json({items:[{id:1,revision:1,name:'本机',protocol:'ollama',base_url:'http://localhost:11434',
      credential_ref:'AGENT_BOT_PROVIDER_TEST_KEY',credential_available:true,enabled:false,description:''}],next_cursor:null})));
  render(<Providers token="offline" />);
  fireEvent.click(await screen.findByRole('button', {name:'编辑本机'}));
  fireEvent.change(screen.getByLabelText('供应商名称'), {target:{value:'修改后的供应商'}});
  fireEvent.click(screen.getByRole('button', {name:'保存供应商'}));
  expect(await screen.findByText('配置冲突')).toBeInTheDocument();
  expect(screen.getByLabelText('供应商名称')).toHaveValue('修改后的供应商');
  expect(screen.getByLabelText('凭据环境变量')).toHaveValue('AGENT_BOT_PROVIDER_TEST_KEY');
});
