import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { AgentChat } from './AgentChat';

it('停止与退出取消请求，工具前草稿不进入成功历史',async()=>{
  let receivedSignal:AbortSignal|undefined;let stream:ReadableStreamDefaultController<Uint8Array>;
  const fetcher=vi.fn(async (url,init)=>{
    if(String(url).includes('agent-runs/stream')){receivedSignal=init.signal;return new Response(new ReadableStream({start(c){stream=c;}}));}
    return Response.json({items:[{id:1,code:'helper',name:'助手',enabled:true}],next_cursor:null});
  });
  vi.stubGlobal('fetch',fetcher);const view=render(<AgentChat token="offline"/>);
  await screen.findByRole('option',{name:'助手'});
  fireEvent.change(screen.getByLabelText('选择 Agent'),{target:{value:'helper'}});
  fireEvent.change(screen.getByLabelText('发送给 Agent'),{target:{value:'hello'}});
  fireEvent.click(screen.getByRole('button',{name:'运行 Agent'}));
  await waitFor(()=>expect(receivedSignal).toBeDefined());
  const id='a'.repeat(32);const emit=(event:object)=>stream!.enqueue(new TextEncoder().encode(`data: ${JSON.stringify({execution_id:id,turn:1,...event})}\n\n`));
  emit({kind:'start',sequence:1});emit({kind:'model_start',sequence:2});emit({kind:'text_delta',sequence:3,delta:'工具前草稿'});emit({kind:'tool_start',sequence:4,tool:'lookup',call_id:'c1'});
  expect(await screen.findByText('工具前草稿')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button',{name:'停止'}));
  await waitFor(()=>expect(receivedSignal?.aborted).toBe(true));
  expect(await screen.findByText('已停止')).toBeInTheDocument();
  expect(screen.getByTestId('agent-history')).toBeEmptyDOMElement();
  fireEvent.click(screen.getByRole('button',{name:'运行 Agent'}));
  await waitFor(()=>expect(fetcher.mock.calls.filter(([url])=>String(url).includes('agent-runs/stream')).length).toBe(2));
  view.unmount();expect(receivedSignal?.aborted).toBe(true);
});

it.each(['error','eof'])('附件后 %s 保留文件但不提交成功历史',async(end)=>{
 const asset={id:'b'.repeat(32),purpose:'output',mime_type:'image/png',size_bytes:80,sha256:'c'.repeat(64),status:'ready',created_at:'2026-10-12T00:00:00',expires_at:'2026-10-13T00:00:00'};
 const id='a'.repeat(32);const events=[{kind:'start',sequence:1},{kind:'artifact',sequence:2,attachment:asset},...(end==='error'?[{kind:'error',sequence:3,code:'MODEL_FAILED',message:'模型失败'}]:[])];
 URL.createObjectURL=vi.fn(()=> 'blob:artifact');URL.revokeObjectURL=vi.fn();
 vi.stubGlobal('fetch',vi.fn(async(url)=>{
  if(String(url).includes('agent-runs/stream'))return new Response(events.map(e=>`data: ${JSON.stringify({execution_id:id,turn:1,...e})}\n\n`).join(''));
  if(String(url).endsWith('/content'))return new Response('image');
  return Response.json({items:[{id:1,code:'helper',name:'助手',enabled:true}],next_cursor:null});
 }));
 render(<AgentChat token="offline"/>);await screen.findByRole('option',{name:'助手'});
 fireEvent.change(screen.getByLabelText('选择 Agent'),{target:{value:'helper'}});fireEvent.change(screen.getByLabelText('发送给 Agent'),{target:{value:'画图'}});fireEvent.click(screen.getByRole('button',{name:'运行 Agent'}));
 expect(await screen.findByText('工具已生成，回复未完成')).toBeVisible();expect(await screen.findByRole('img',{name:'生成图片'})).toBeVisible();expect(screen.getByTestId('agent-history')).toBeEmptyDOMElement();
});

it('生成中换 Agent 会取消旧执行并清空旧会话',async()=>{
 let signal:AbortSignal|undefined;
 vi.stubGlobal('fetch',vi.fn(async(url,init)=>{
  if(String(url).includes('agent-runs/stream')){signal=init.signal;return new Response(new ReadableStream({/** 保持流未完成以验证切换取消。 */start(){}}));}
  return Response.json({items:[{id:1,code:'helper',name:'助手',enabled:true},{id:2,code:'other',name:'另一助手',enabled:true}],next_cursor:null});
 }));render(<AgentChat token="offline"/>);await screen.findByRole('option',{name:'助手'});
 fireEvent.change(screen.getByLabelText('选择 Agent'),{target:{value:'helper'}});fireEvent.change(screen.getByLabelText('发送给 Agent'),{target:{value:'画图'}});fireEvent.click(screen.getByRole('button',{name:'运行 Agent'}));
 await waitFor(()=>expect(signal).toBeDefined());expect(screen.getByLabelText('选择 Agent')).toBeEnabled();fireEvent.change(screen.getByLabelText('选择 Agent'),{target:{value:'other'}});expect(signal!.aborted).toBe(true);expect(screen.getByTestId('agent-history')).toBeEmptyDOMElement();
});

it('同步纯图片请求只附本轮 ID，下一轮历史保持纯文本',async()=>{
 const inputAsset={id:'b'.repeat(32),purpose:'input',mime_type:'image/png',size_bytes:80,sha256:'c'.repeat(64),status:'ready',created_at:'2026-10-12T00:00:00',expires_at:'2026-10-13T00:00:00'};const bodies:any[]=[];
 vi.stubGlobal('fetch',vi.fn(async(url,init)=>{
  if(String(url).includes('/media/uploads'))return Response.json(inputAsset);
  if(String(url).endsWith('/agent-runs')){bodies.push(JSON.parse(init.body));return Response.json({execution_id:'a'.repeat(32),content:'识别完成',model:'vision',record_saved:true,attachments:[]});}
  return Response.json({items:[{id:1,code:'helper',name:'助手',enabled:true}],next_cursor:null});
 }));render(<AgentChat token="offline"/>);await screen.findByRole('option',{name:'助手'});fireEvent.change(screen.getByLabelText('选择 Agent'),{target:{value:'helper'}});fireEvent.click(screen.getByLabelText('流式执行'));
 fireEvent.change(screen.getByLabelText('添加图片'),{target:{files:[new File(['png'],'a.png',{type:'image/png'})]}});await screen.findByText(/a.png · 已上传/);fireEvent.click(screen.getByRole('button',{name:'运行 Agent'}));await screen.findByText('识别完成');
 expect(bodies[0].attachment_ids).toEqual([inputAsset.id]);expect(bodies[0].prompt).toBeTruthy();fireEvent.change(screen.getByLabelText('发送给 Agent'),{target:{value:'继续'}});fireEvent.click(screen.getByRole('button',{name:'运行 Agent'}));await waitFor(()=>expect(bodies).toHaveLength(2));expect(bodies[1].attachment_ids).toEqual([]);expect(bodies[1].messages).toHaveLength(2);expect(JSON.stringify(bodies[1].messages)).not.toContain(inputAsset.id);
});
