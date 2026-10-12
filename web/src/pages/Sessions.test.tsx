import { fireEvent, render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { Sessions } from './Sessions';

it('列表不加载正文，详情按需请求', async () => {
  const requests: string[] = [];
  const record = {execution_id:'a'.repeat(32),session_id:'admin_test',request_id:'r',model:'exact/model',configured_model:'local',status:'completed',duration_ms:24,usage:{total_tokens:null},created_at:'2026-10-12T00:00:00'};
  vi.stubGlobal('fetch', vi.fn(async (url) => {requests.push(String(url));return Response.json(String(url).includes('?') ? {items:[record],next_cursor:null} : {...record,prompt:'私有输入',content:'保存的回复'});}));
  render(<Sessions token="offline"/>);
  fireEvent.click(await screen.findByRole('button',{name:'查看详情'}));
  expect(await screen.findByText('保存的回复')).toBeInTheDocument();
  expect(requests).toHaveLength(2);
});
