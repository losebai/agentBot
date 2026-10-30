import { render, screen } from '@testing-library/react';
import { expect, it } from 'vitest';
import { NapCatStatus } from './NapCatStatus';

it('进程运行且无机器人连接时等待登录，不能误报 QQ 在线', () => {
  render(<NapCatStatus status={{ mode: 'managed', state: 'running', webui_ready: true, error_code: null }} botCount={0} />);
  expect(screen.getByText('等待登录或连接')).toBeInTheDocument();
  expect(screen.queryByText('QQ 已在线')).not.toBeInTheDocument();
  expect(screen.queryByRole('button')).not.toBeInTheDocument();
});

it('只按真实机器人连接标记在线', () => {
  render(<NapCatStatus status={{ mode: 'managed', state: 'running', webui_ready: true, error_code: null }} botCount={1} />);
  expect(screen.getByText('QQ 已在线')).toBeInTheDocument();
});

it('缺运行载荷时给出明确提示', () => {
  render(<NapCatStatus status={{ mode: 'managed', state: 'failed', webui_ready: false, error_code: 'PAYLOAD_INVALID' }} botCount={0} />);
  expect(screen.getByText(/运行载荷缺失或校验失败/)).toBeInTheDocument();
});
