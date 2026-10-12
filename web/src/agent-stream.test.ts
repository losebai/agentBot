import { expect, it } from 'vitest';
import { streamAgent } from './agent-stream';

const id='a'.repeat(32);
const start={kind:'start',execution_id:id,sequence:1,turn:0};
const done={kind:'done',execution_id:id,sequence:2,turn:1,metadata:{execution_id:id,record_saved:true}};
const frame=(v:unknown)=>`data: ${JSON.stringify(v)}\n\n`;
async function collect(text:string){const items=[];for await(const event of streamAgent(new Response(text)))items.push(event);return items;}
it('done 后 EOF 保留成功',async()=>expect((await collect(frame(start)+frame(done))).at(-1)?.kind).toBe('done'));
it('缺失或重复终态不能成功',async()=>{await expect(collect(frame(start))).rejects.toThrow();await expect(collect(frame(start)+frame(done)+frame(done))).rejects.toThrow();});
it('拒绝污染执行 ID 和乱序',async()=>{await expect(collect(frame(start)+frame({...done,execution_id:'b'.repeat(32)}))).rejects.toThrow();await expect(collect(frame(start)+frame({...done,sequence:1}))).rejects.toThrow();});
it('处理 UTF-8 分片并释放 reader',async()=>{
 const bytes=new TextEncoder().encode(frame(start)+frame({kind:'text_delta',execution_id:id,sequence:2,turn:1,delta:'你好'})+frame({...done,sequence:3}));
 const response=new Response(new ReadableStream({start(controller){for(const byte of bytes)controller.enqueue(new Uint8Array([byte]));controller.close();}}));
 const items=[];for await(const event of streamAgent(response))items.push(event);
 expect(items[1].delta).toBe('你好');expect(response.body?.locked).toBe(false);
});

const asset={id:'b'.repeat(32),purpose:'output',mime_type:'image/png',size_bytes:80,sha256:'c'.repeat(64),status:'ready',created_at:'2026-10-12T00:00:00',expires_at:'2026-10-13T00:00:00'};
const artifact={kind:'artifact',execution_id:id,sequence:2,turn:1,attachment:asset};
it('附件事件与终态匹配，缺失终态仍保留已发出的产物',async()=>{
 const result=await collect(frame(start)+frame(artifact)+frame({...done,sequence:3,metadata:{...done.metadata,attachments:[asset]}}));expect(result[1].kind).toBe('artifact');
 const received=[];let failure:unknown;try{for await(const event of streamAgent(new Response(frame(start)+frame(artifact))))received.push(event);}catch(e){failure=e;}
 expect(received.at(-1)?.kind).toBe('artifact');expect(failure).toBeInstanceOf(Error);
});
it('拒绝 URL、内联内容、越限或不一致附件',async()=>{
 for(const invalid of [{...asset,url:'https://external.test/a'},{...asset,data:'base64'},{...asset,size_bytes:11*1024*1024},{...asset,purpose:'input'},{...asset,status:'expired'},{...asset,id:'../file'}]){
  await expect(collect(frame(start)+frame({...artifact,attachment:invalid}))).rejects.toThrow(/附件/);
 }
 await expect(collect(frame(start)+frame(artifact)+frame({...done,sequence:3,metadata:{...done.metadata,attachments:[]}}))).rejects.toThrow(/附件/);
});
