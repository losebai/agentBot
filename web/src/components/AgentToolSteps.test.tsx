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

it('预检失败明确展示工具未执行、错误字段与规则，同批跳过不会显示成功', () => {
  render(<AgentToolSteps steps={[{
    execution_id: 'a'.repeat(32), sequence: 4, turn: 3, kind: 'tool_result', tool: 'web_search',
    call_id: 'invalid', status: 'failed', duration_ms: 0, summary: 'AGENT_TOOL_ARGUMENTS_INVALID',
    arguments: {numResults: 99}, result: {phase: 'validation', executed: false,
      validation_errors: [{field: 'numResults', rule: 'maximum', message: '不能大于 5'}]},
  }, {
    execution_id: 'a'.repeat(32), sequence: 5, turn: 3, kind: 'tool_result', tool: 'read_url',
    call_id: 'skipped', status: 'failed', duration_ms: 0, summary: 'AGENT_TOOL_BATCH_REJECTED',
    result: {phase: 'validation', executed: false, validation_errors: []},
  }]} />);
  expect(screen.getByText(/参数校验失败/)).toBeVisible();
  expect(screen.getByText(/同批取消/)).toBeVisible();
  expect(screen.getAllByText(/工具未执行/)).toHaveLength(2);
  expect(screen.getByText('numResults · maximum：不能大于 5')).toBeVisible();
  expect(screen.queryByText(/已完成/)).not.toBeInTheDocument();
});
