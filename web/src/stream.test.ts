import { expect, it } from 'vitest';
import { readChatEvents } from './stream';

function response(text: string) {
  const bytes = new TextEncoder().encode(text);
  return new Response(new ReadableStream({ start(controller) {
    for (const byte of bytes) controller.enqueue(new Uint8Array([byte]));
    controller.close();
  } }));
}

it('跨字节中文与完成事件正确解析', async () => {
  const text = 'event: delta\r\ndata: {"kind":"delta","request_id":"r","delta":"你好"}\r\n\r\n' +
    'event: done\ndata: {"kind":"done","request_id":"r","metadata":{"record_saved":true}}\n\n';
  const events = [];
  for await (const event of readChatEvents(response(text), new AbortController().signal)) events.push(event);
  expect(events.map(event => event.kind)).toEqual(['delta', 'done']);
  expect(events[0].delta).toBe('你好');
});

it('缺少完成标志的断流不当作成功', async () => {
  const consume = async () => { for await (const _event of readChatEvents(response(
    'data: {"kind":"delta","request_id":"r","delta":"部分"}\n\n'), new AbortController().signal)) { /* consume */ } };
  await expect(consume()).rejects.toThrow(/未完成/);
});
