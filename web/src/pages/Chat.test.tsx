import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { Chat } from './Chat';

describe('普通文本对话', () => {
  it('显示真实回复，历史中携带上一轮消息', async () => {
    const requests: { prompt: string; messages: { role: string; content: string }[] }[] = [];
    vi.stubGlobal('fetch', vi.fn(async (_url, init) => {
      requests.push(JSON.parse(init.body));
      return Response.json({ content: '实际回复', request_id: 'offline-id' });
    }));
    render(<Chat token="offline-token" />);
    fireEvent.change(screen.getByLabelText('消息'), { target: { value: '你好' } });
    fireEvent.click(screen.getByRole('button', { name: '发送消息' }));
    expect(await screen.findByText('实际回复')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('消息'), { target: { value: '继续' } });
    fireEvent.click(screen.getByRole('button', { name: '发送消息' }));
    await screen.findAllByText('实际回复');
    expect(requests[1].messages).toEqual([
      { role: 'user', content: '你好' }, { role: 'assistant', content: '实际回复' },
    ]);
  });

  it('请求失败后恢复提交，保留输入，不伪造回答', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => Response.json({ code: 'AI_UNAVAILABLE',
      message: 'AI 暂时不可用', request_id: 'offline-id' }, { status: 503 })));
    render(<Chat token="offline-token" />);
    fireEvent.change(screen.getByLabelText('消息'), { target: { value: '你好' } });
    fireEvent.click(screen.getByRole('button', { name: '发送消息' }));
    expect(await screen.findByText('AI 暂时不可用')).toBeInTheDocument();
    expect(screen.getByLabelText('消息')).toHaveValue('你好');
    expect(screen.getByRole('button', { name: '发送消息' })).toBeEnabled();
    expect(screen.queryByText('实际回复')).not.toBeInTheDocument();
  });
});

it('流式停止保留部分回复且不放入下一次历史，退出取消请求', async () => {
  let streamController: ReadableStreamDefaultController<Uint8Array>;
  const bodies: {messages: unknown[]}[] = [];
  let streamSignal: AbortSignal;
  const encoder = new TextEncoder();
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    if(String(url).endsWith('/status')) return Response.json({plain_ai_backend:'python',configured_model:'local'});
    if(String(url).includes('/ai/models')) return Response.json({items:[{id:1,alias:'local',enabled:true}],next_cursor:null});
    if(String(url).endsWith('/chat/stream')) {bodies.push(JSON.parse(init.body));streamSignal=init.signal;return new Response(new ReadableStream({start(controller){streamController=controller;controller.enqueue(encoder.encode('data: {"kind":"delta","request_id":"r","delta":"部分回复"}\n\n'));}}));}
    return Response.json({content:'完整回复',request_id:'r'});
  }));
  const view=render(<Chat token="offline"/>);
  fireEvent.click(await screen.findByLabelText('流式输出'));
  fireEvent.change(screen.getByLabelText('消息'),{target:{value:'第一条'}});
  fireEvent.click(screen.getByRole('button',{name:'发送消息'}));
  expect(await screen.findByText('部分回复')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button',{name:'停止生成'}));
  expect(await screen.findByText(/已停止/)).toBeInTheDocument();
  expect(streamSignal!.aborted).toBe(true);
  fireEvent.change(screen.getByLabelText('消息'),{target:{value:'第二条'}});
  fireEvent.click(screen.getByRole('button',{name:'发送消息'}));
  await screen.findByRole('button',{name:'停止生成'});
  expect(bodies[1].messages).toEqual([]);
  view.unmount();
  expect(streamSignal!.aborted).toBe(true);
  void streamController!;
});
