import type { RunStep } from '../agent-types';

const statusNames = { started: '执行中', completed: '已完成', failed: '失败', unknown: '结果未知' };

/** 按调用 ID 合并开始与返回事件，保留未结束调用及历史缺失字段。 */
function toolCalls(steps: RunStep[]): RunStep[] {
  const calls = new Map<string, RunStep>();
  for (const step of steps) {
    if (!step.tool) continue;
    const key = step.call_id ?? `step-${step.sequence}`;
    const previous = calls.get(key);
    calls.set(key, {
      ...previous, ...step,
      arguments: step.arguments ?? previous?.arguments ?? null,
      result: step.result ?? previous?.result ?? null,
    });
  }
  return [...calls.values()];
}

/** JSON 工具正文可展开阅读；普通正文保持原文，不执行 HTML 或 Markdown。 */
function readable(value: unknown): string {
  if (typeof value === 'string') {
    try { return JSON.stringify(JSON.parse(value), null, 2); }
    catch { return value; }
  }
  return JSON.stringify(value, null, 2);
}

/** 只读取服务端已脱敏的错误说明，历史记录缺失字段时继续展示原始返回。 */
function validationIssues(call: RunStep): {field: string; rule: string; message: string}[] {
  const value = call.result?.validation_errors;
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is {field: string; rule: string; message: string} =>
    item !== null && typeof item === 'object' && !Array.isArray(item)
    && typeof item.field === 'string' && typeof item.rule === 'string' && typeof item.message === 'string');
}

/** 两个历史详情入口复用只读工具视图，展示每次调用的入参、正文和返回状态。 */
export function AgentToolSteps({ steps }: { steps: RunStep[] }) {
  const calls = toolCalls(steps);
  if (!calls.length) return <p>没有保存的工具调用。</p>;
  return <ol className="agent-step-list tool-call-records">{calls.map(call => <li key={call.call_id ?? call.sequence}>
    <p>第 {call.turn} 轮 · {call.tool} · {call.call_id ?? '无调用 ID'} · {call.result?.phase === 'validation'
      ? call.summary === 'AGENT_TOOL_ARGUMENTS_INVALID' ? '参数校验失败' : '同批取消' : statusNames[call.status]}
      {call.duration_ms != null ? ` · ${call.duration_ms} ms` : ''}</p>
    {call.result?.phase === 'validation' && <div>
      <p>工具未执行</p>
      {validationIssues(call).map((issue, index) => <p key={index}>{issue.field} · {issue.rule}：{issue.message}</p>)}
    </div>}
    {call.summary && <p>{call.summary}</p>}
    <details open><summary>入参</summary><pre>{call.arguments == null ? '未保存入参' : readable(call.arguments)}</pre></details>
    <details open><summary>返回值</summary>{call.result == null ? <p>未记录返回值</p> : <>
      {Object.hasOwn(call.result, 'content') && <pre>{readable(call.result.content)}</pre>}
      <pre>{readable(Object.fromEntries(Object.entries(call.result).filter(([key]) => key !== 'content')))}</pre>
    </>}</details>
  </li>)}</ol>;
}
