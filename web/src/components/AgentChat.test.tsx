import { useRunningPluginFixture } from '../test-plugins';
import { chooseSelect, selectDisplay } from '../test-select';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { AgentChat } from './AgentChat';

useRunningPluginFixture();

it('自动选中第一个启用的 Agent，无需手动选择就能运行', async () => {
  let body: any;
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    if (String(url).endsWith('/agent-runs')) {
      body = JSON.parse(init.body);
      return Response.json({execution_id: 'a'.repeat(32), content: '默认助手已处理', model: 'chat', record_saved: true});
    }
    return Response.json({items: [
      {id: 1, code: 'disabled', name: '停用助手', enabled: false},
      {id: 2, code: 'first', name: '首个可用助手', enabled: true},
      {id: 3, code: 'second', name: '另一助手', enabled: true},
    ], next_cursor: null});
  }));
  render(<AgentChat token="offline"/>);
  await waitFor(() => expect(selectDisplay(screen.getByRole('combobox', {name: '选择 Agent'}))).toHaveTextContent('首个可用助手'));
  fireEvent.click(screen.getByLabelText('流式执行'));
  fireEvent.change(screen.getByLabelText('发送给 Agent'), {target: {value: '你好'}});
  fireEvent.click(screen.getByRole('button', {name: '运行 Agent'}));
  await screen.findByText('默认助手已处理');
  expect(body.agent_code).toBe('first');
});

it('分页首次出现可用 Agent 时自动选择，后续分页保留手动选择', async () => {
  vi.stubGlobal('fetch', vi.fn(async url => {
    const path = String(url);
    return Response.json(path.includes('cursor=last')
      ? {items: [{id: 4, code: 'last', name: '最后助手', enabled: true}], next_cursor: null}
      : path.includes('cursor=next')
        ? {items: [{id: 2, code: 'first', name: '首个可用助手', enabled: true},
          {id: 3, code: 'chosen', name: '手选助手', enabled: true}], next_cursor: 'last'}
        : {items: [{id: 1, code: 'disabled', name: '停用助手', enabled: false}], next_cursor: 'next'});
  }));
  render(<AgentChat token="offline"/>);
  fireEvent.click(await screen.findByRole('button', {name: '加载更多 Agent'}));
  await waitFor(() => expect(selectDisplay(screen.getByRole('combobox', {name: '选择 Agent'}))).toHaveTextContent('首个可用助手'));
  await chooseSelect(screen.getByRole('combobox', {name: '选择 Agent'}), '手选助手');
  fireEvent.click(screen.getByRole('button', {name: '加载更多 Agent'}));
  await waitFor(() => expect(screen.queryByRole('button', {name: '加载更多 Agent'})).not.toBeInTheDocument());
  expect(selectDisplay(screen.getByRole('combobox', {name: '选择 Agent'}))).toHaveTextContent('手选助手');
});

it('统一入口混选图片和视频，2000字提示保留且两类资产分别传参', async () => {
  const asset = { id: 'd'.repeat(32), purpose: 'input', mime_type: 'video/mp4', size_bytes: 10,
    sha256: 'e'.repeat(64), status: 'ready', created_at: '2026-10-27T00:00:00', expires_at: '2026-10-28T00:00:00' };
  const image = {...asset, id: 'f'.repeat(32), mime_type: 'image/png'};
  let body: any;
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    if (String(url).includes('/media/video/uploads')) return Response.json(asset);
    if (String(url).includes('/media/uploads')) return Response.json(image);
    if (String(url).endsWith('/agent-runs')) {
      body = JSON.parse(init.body);
      return Response.json({ execution_id: 'a'.repeat(32), content: '已处理', model: 'chat', record_saved: true });
    }
    return Response.json({ items: [{ id: 1, code: 'helper', name: '助手', enabled: true }], next_cursor: null });
  }));
  render(<AgentChat token="offline" />);
  await chooseSelect(screen.getByRole('combobox', { name: '选择 Agent' }), '助手');
  fireEvent.click(screen.getByLabelText('流式执行'));
  fireEvent.change(screen.getByLabelText('添加文件'), {
    target: { files: [new File(['video'], '投稿.mp4', { type: 'video/mp4' }),
      new File(['png'], '封面.png', {type: 'image/png'})] },
  });
  await screen.findByText('封面.png · 已上传');
  expect(screen.getByText('投稿.mp4 · 已上传')).toBeVisible();
  expect(document.querySelectorAll('input[type="file"]')).toHaveLength(1);
  fireEvent.change(screen.getByLabelText('发送给 Agent'), { target: { value: '文'.repeat(2000) } });
  fireEvent.click(screen.getByRole('button', { name: '运行 Agent' }));
  await screen.findByText('已处理');
  expect(body.prompt).toHaveLength(2000);
  expect(body.video_asset_id).toBe(asset.id);
  expect(body.attachment_ids).toEqual([image.id]);
});

it('停止与退出取消请求，工具前草稿不进入成功历史',async()=>{
  let receivedSignal:AbortSignal|undefined;let stream:ReadableStreamDefaultController<Uint8Array>;
  const fetcher=vi.fn(async (url,init)=>{
    if(String(url).includes('agent-runs/stream')){receivedSignal=init.signal;return new Response(new ReadableStream({start(c){stream=c;}}));}
    return Response.json({items:[{id:1,code:'helper',name:'助手',enabled:true}],next_cursor:null});
  });
  vi.stubGlobal('fetch',fetcher);const view=render(<AgentChat token="offline"/>);

  await chooseSelect(screen.getByRole('combobox', { name: '选择 Agent' }), '助手');
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
 render(<AgentChat token="offline"/>);
 await chooseSelect(screen.getByRole('combobox', { name: '选择 Agent' }), '助手');fireEvent.change(screen.getByLabelText('发送给 Agent'),{target:{value:'画图'}});fireEvent.click(screen.getByRole('button',{name:'运行 Agent'}));
 expect(await screen.findByText('工具已生成，回复未完成')).toBeVisible();expect(await screen.findByRole('img',{name:'生成图片'})).toBeVisible();expect(screen.getByTestId('agent-history')).toBeEmptyDOMElement();
});

it('生成中换 Agent 会取消旧执行并清空旧会话',async()=>{
 let signal:AbortSignal|undefined;
 vi.stubGlobal('fetch',vi.fn(async(url,init)=>{
  if(String(url).includes('agent-runs/stream')){signal=init.signal;return new Response(new ReadableStream({/** 保持流未完成以验证切换取消。 */start(){}}));}
  return Response.json({items:[{id:1,code:'helper',name:'助手',enabled:true},{id:2,code:'other',name:'另一助手',enabled:true}],next_cursor:null});
 }));render(<AgentChat token="offline"/>);
 await chooseSelect(screen.getByRole('combobox', { name: '选择 Agent' }), '助手');fireEvent.change(screen.getByLabelText('发送给 Agent'),{target:{value:'画图'}});fireEvent.click(screen.getByRole('button',{name:'运行 Agent'}));
 await waitFor(()=>expect(signal).toBeDefined());expect(screen.getByRole('combobox', { name: '选择 Agent' })).toBeEnabled();await chooseSelect(screen.getByRole('combobox', { name: '选择 Agent' }), '另一助手');expect(signal!.aborted).toBe(true);expect(screen.getByTestId('agent-history')).toBeEmptyDOMElement();
});

it('同步纯图片请求只附本轮 ID，下一轮历史保持纯文本',async()=>{
 const inputAsset={id:'b'.repeat(32),purpose:'input',mime_type:'image/png',size_bytes:80,sha256:'c'.repeat(64),status:'ready',created_at:'2026-10-12T00:00:00',expires_at:'2026-10-13T00:00:00'};const bodies:any[]=[];
 vi.stubGlobal('fetch',vi.fn(async(url,init)=>{
  if(String(url).includes('/media/uploads'))return Response.json(inputAsset);
  if(String(url).endsWith('/agent-runs')){bodies.push(JSON.parse(init.body));return Response.json({execution_id:'a'.repeat(32),content:'识别完成',model:'vision',record_saved:true,attachments:[]});}
  return Response.json({items:[{id:1,code:'helper',name:'助手',enabled:true}],next_cursor:null});
 }));render(<AgentChat token="offline"/>);await chooseSelect(screen.getByRole('combobox', { name: '选择 Agent' }), '助手');fireEvent.click(screen.getByLabelText('流式执行'));
 fireEvent.change(screen.getByLabelText('添加文件'),{target:{files:[new File(['png'],'a.png',{type:'image/png'})]}});await screen.findByText(/a.png · 已上传/);fireEvent.click(screen.getByRole('button',{name:'运行 Agent'}));await screen.findByText('识别完成');
 expect(bodies[0].attachment_ids).toEqual([inputAsset.id]);expect(bodies[0].prompt).toBeTruthy();fireEvent.change(screen.getByLabelText('发送给 Agent'),{target:{value:'继续'}});fireEvent.click(screen.getByRole('button',{name:'运行 Agent'}));await waitFor(()=>expect(bodies).toHaveLength(2));expect(bodies[1].attachment_ids).toEqual([]);expect(bodies[1].messages).toHaveLength(2);expect(JSON.stringify(bodies[1].messages)).not.toContain(inputAsset.id);
});
