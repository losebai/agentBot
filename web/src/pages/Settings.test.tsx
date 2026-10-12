import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { Settings } from './Settings';

const snapshot = {
  database_revision: 1, applied_revision: 1, restart_required_fields: [],
  fields: [
    { name: 'dispatch_chat_llm_temperature', description: '温度', kind: 'number', value: 0.7,
      nullable: true, secret_is_set: false, restart_required: false },
    { name: 'dispatch_chat_ai_token', description: 'AI 密钥', kind: 'secret', value: null,
      nullable: true, secret_is_set: true, restart_required: false },
  ],
};

describe('配置编辑', () => {
  it('只发送已编辑字段，保留未编辑密钥并显示保存与应用版本', async () => {
    let submitted: unknown;
    vi.stubGlobal('fetch', vi.fn(async (_url, init) => {
      if (init?.method === 'PATCH') {
        submitted = JSON.parse(init.body);
        return Response.json({ ...snapshot, database_revision: 2,
          restart_required_fields: ['port'] });
      }
      return Response.json(snapshot);
    }));
    render(<Settings token="offline-token" />);
    fireEvent.change(await screen.findByLabelText('温度'), { target: { value: '0.4' } });
    fireEvent.click(screen.getByRole('button', { name: '保存修改' }));
    await waitFor(() => expect(submitted).toEqual({ expected_revision: 1,
      values: { dispatch_chat_llm_temperature: 0.4 } }));
    expect(await screen.findByText(/数据库版本 2/)).toBeInTheDocument();
    expect(screen.getByText(/需要重启：port/)).toBeInTheDocument();
  });

  it('版本冲突后保留编辑内容并提示重新加载', async () => {
    vi.stubGlobal('fetch', vi.fn(async (_url, init) => init?.method === 'PATCH'
      ? Response.json({ code: 'SETTINGS_CONFLICT', message: '配置已被修改，请重新加载',
        request_id: 'offline-id' }, { status: 409 })
      : Response.json(snapshot)));
    render(<Settings token="offline-token" />);
    fireEvent.change(await screen.findByLabelText('温度'), { target: { value: '0.4' } });
    fireEvent.click(screen.getByRole('button', { name: '保存修改' }));
    expect(await screen.findByText('配置已被修改，请重新加载')).toBeInTheDocument();
    expect(screen.getByLabelText('温度')).toHaveValue(0.4);
  });
});
