import { chooseSelect } from '../test-select';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { Sessions } from './Sessions';

it('列表不加载正文，详情按需请求', async () => {
  const requests: string[] = [];
  const record = {id:1,type:'ordinary',source:'python:agent_bot_ai_session',source_id:'a',execution_id:'a'.repeat(32),session_Id:'admin_test',request_id:'r',model:'exact/model',status:'completed',duration:24,create_time:'2026-10-12T00:00:00'};
  vi.stubGlobal('fetch', vi.fn(async (url) => {requests.push(String(url));return Response.json(String(url).includes('?') ? {items:[record],next_cursor:null} : {...record,content:'私有输入',replay_context:'保存的回复',request_context:null,data:null,user_certificate:null});}));
  render(<Sessions token="offline"/>);
  fireEvent.click(await screen.findByRole('button',{name:'查看详情'}));
  expect(await screen.findByText('保存的回复')).toBeInTheDocument();
  expect(requests).toHaveLength(2);
  expect(requests[0]).toContain('/ai/records?');
});

it('普通和 Agent 共用类型筛选', async () => {
  const requests: string[] = [];
  vi.stubGlobal('fetch', vi.fn(async url => {
    requests.push(String(url)); return Response.json({items: [], next_cursor: null});
  }));
  render(<Sessions token="offline" />);
  await waitFor(() => expect(screen.getByRole('button', {name: /筛\s*选/})).toBeEnabled());
  await chooseSelect(screen.getByRole('combobox', {name: '类型'}), 'Agent 调用');
  fireEvent.click(screen.getByRole('button', {name: /筛\s*选/}));
  await waitFor(() => expect(requests.at(-1)).toContain('type=agent'));
});
