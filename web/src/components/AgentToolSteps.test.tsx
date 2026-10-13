import { render, screen } from '@testing-library/react';
import { expect, it } from 'vitest';
import { AgentToolSteps } from './AgentToolSteps';
import type { RunStep } from '../agent-types';

it('按调用 ID 配对入参和返回，两个同名工具不会相互覆盖', () => {
  const common = { execution_id: 'a'.repeat(32), turn: 1, tool: 'lookup', duration_ms: 10, summary: '' };
  const steps = [
    { ...common, sequence: 1, call_id: 'c1', kind: 'tool_start', status: 'started', arguments: { city: '上海' }, result: null },
    { ...common, sequence: 2, call_id: 'c1', kind: 'tool_result', status: 'completed', arguments: { city: '上海' }, result: { content: '{"temperature":23}', is_error: false } },
    { ...common, sequence: 3, call_id: 'c2', kind: 'tool_start', status: 'started', arguments: { city: '北京' }, result: null },
    { ...common, sequence: 4, call_id: 'c2', kind: 'tool_result', status: 'failed', arguments: { city: '北京' }, result: { content: '服务拒绝请求', is_error: true } },
  ] as RunStep[];
  render(<AgentToolSteps steps={steps} />);
  expect(screen.getAllByText('入参')).toHaveLength(2);
  expect(screen.getAllByText('返回值')).toHaveLength(2);
  expect(screen.getByText(/"city": "上海"/)).toBeInTheDocument();
  expect(screen.getByText(/"temperature": 23/)).toBeInTheDocument();
  expect(screen.getByText(/服务拒绝请求/)).toBeInTheDocument();
  expect(screen.getByText(/c1/)).toBeInTheDocument();
  expect(screen.getByText(/c2/)).toBeInTheDocument();
});

it('历史未保存或工具未返回时明确显示缺失，不制造空对象返回', () => {
  render(<AgentToolSteps steps={[{ execution_id: 'a'.repeat(32), sequence: 1, turn: 1,
    tool: 'old', call_id: 'legacy', kind: 'tool_result', status: 'unknown', summary: '', duration_ms: 0,
    arguments: null, result: null }]} />);
  expect(screen.getByText('未保存入参')).toBeInTheDocument();
  expect(screen.getByText('未记录返回值')).toBeInTheDocument();
});
