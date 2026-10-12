import type { AgentEvent } from './agent-types';
import type { MediaAsset } from './media-types';
import { validateArtifact } from './media-validation';

/** 有界解析事件流；调用方必须等终态后的 EOF 才提交成功历史。 */
export async function* streamAgent(response:Response, signal?:AbortSignal):AsyncGenerator<AgentEvent> {
  if(!response.body)throw new Error('服务未返回 Agent 流');
  const reader=response.body.getReader(), decoder=new TextDecoder('utf-8',{fatal:true}), encoder=new TextEncoder();
  let pending='',lines:string[]=[],bytes=0,total=0,sequence=0,id='',terminal=false;
  const artifacts:MediaAsset[]=[];
  const abort=()=>{void reader.cancel();};signal?.addEventListener('abort',abort,{once:true});
  /** 附件与序号一起校验，终态必须准确列出此前公布的产物。 */
  function parse():AgentEvent|null {
    if(!lines.length)return null;
    const value:unknown=JSON.parse(lines.join('\n'));lines=[];
    if(!value||typeof value!=='object')throw new Error('Agent 事件无效');
    const event=value as AgentEvent;
    if(terminal||!['start','model_start','text_delta','tool_start','tool_result','artifact','warning','done','error'].includes(event.kind)
      ||typeof event.execution_id!=='string'||!/^[0-9a-f]{32}$/.test(event.execution_id)
      ||!Number.isInteger(event.sequence)||event.sequence<=sequence||!Number.isInteger(event.turn)||event.turn<0||event.turn>6)
      throw new Error('Agent 事件顺序或格式无效');
    if(!id){if(event.kind!=='start'&&event.kind!=='error')throw new Error('缺少 Agent 开始事件');id=event.execution_id;}
    else if(event.execution_id!==id||event.kind==='start')throw new Error('Agent 执行标识不匹配');
    if(event.kind==='text_delta'&&typeof event.delta!=='string')throw new Error('正文事件无效');
    if(['tool_start','tool_result'].includes(event.kind)&&(typeof event.tool!=='string'||typeof event.call_id!=='string'))throw new Error('工具事件无效');
    if(event.kind==='done'&&(!event.metadata||event.metadata.execution_id!==id||typeof event.metadata.record_saved!=='boolean'))throw new Error('完成信息无效');
    if(event.kind==='artifact'){validateArtifact(event.attachment);if(artifacts.some(a=>a.id===event.attachment!.id))throw new Error('附件重复');artifacts.push(event.attachment);}
    else if(event.attachment!=null)throw new Error('附件事件无效');
    if(event.kind==='done'){
      const final=event.metadata!.attachments??[];if(!Array.isArray(final)||final.length!==artifacts.length)throw new Error('完成附件不匹配');
      final.forEach((a,index)=>{validateArtifact(a);if(Object.keys(a).some(k=>a[k as keyof MediaAsset]!==artifacts[index][k as keyof MediaAsset]))throw new Error('完成附件不匹配');});
    }
    sequence=event.sequence;terminal=event.kind==='done'||event.kind==='error';return event;
  }
  try{
    while(true){
      if(signal?.aborted)throw new DOMException('已停止','AbortError');
      const chunk=await reader.read();
      if(signal?.aborted)throw new DOMException('已停止','AbortError');
      total+=chunk.value?.byteLength||0;if(total>4194304)throw new Error('Agent 输出超过大小限制');
      pending+=decoder.decode(chunk.value,{stream:!chunk.done});
      while(pending.includes('\n')){
        const index=pending.indexOf('\n'),line=pending.slice(0,index).replace(/\r$/,'');pending=pending.slice(index+1);
        bytes+=encoder.encode(line).byteLength+1;if(bytes>262144)throw new Error('Agent 事件超过大小限制');
        if(!line){const event=parse();bytes=0;if(event)yield event;}else if(line.startsWith('data:'))lines.push(line.slice(5).replace(/^ /,''));
      }
      if(encoder.encode(pending).byteLength+bytes>262144)throw new Error('Agent 事件超过大小限制');
      if(chunk.done)break;
    }
    if(!terminal||pending.trim()||lines.length)throw new Error('Agent 流未完整结束，请查看执行记录');
  }finally{signal?.removeEventListener('abort',abort);await reader.cancel().catch(()=>undefined);reader.releaseLock();}
}
