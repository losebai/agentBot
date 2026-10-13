import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { Settings } from './Settings';

const snapshot = {
  database_revision: 1, applied_revision: 1, restart_required_fields: [],
  fields: [
    { name: 'chat_llm_temperature', description: '温度', kind: 'number', value: 0.7,
      nullable: true, secret_is_set: false, restart_required: false },
    { name: 'chat_naraka_default_role_id', description: '默认角色 ID', kind: 'secret', value: null,
      nullable: true, secret_is_set: true, restart_required: false },
  ],
};

describe('配置编辑', () => {
  it.each([null, false, true])('可空布尔值 %s 能保留原值并显式清除为 null', async initial => {
    let submitted: unknown;
    const fields = [{ name: 'chat_llm_enable_thinking', description: '深度思考', kind: 'boolean',
      value: initial, nullable: true, secret_is_set: false, restart_required: false }];
    vi.stubGlobal('fetch', vi.fn(async (_url, init) => {
      if (init?.method === 'PATCH') submitted = JSON.parse(init.body);
      return Response.json({ ...snapshot, fields });
    }));
    render(<Settings token="offline-token" />);
    const control = await screen.findByRole('combobox', { name: '深度思考' });
    expect(control).toHaveValue(initial === null ? '' : String(initial));
    fireEvent.change(control, { target: { value: 'false' } });
    fireEvent.click(screen.getByRole('button', { name: '保存修改' }));
    await waitFor(() => expect(submitted).toEqual({ expected_revision: 1,
      values: { chat_llm_enable_thinking: false } }));
    await waitFor(() => expect(screen.getByRole('button', { name: '保存修改' })).toBeDisabled());
    fireEvent.change(control, { target: { value: '' } });
    fireEvent.click(screen.getByRole('button', { name: '保存修改' }));
    await waitFor(() => expect(submitted).toEqual({ expected_revision: 1,
      values: { chat_llm_enable_thinking: null } }));
  });

  it('静态配置分组只读，不能修改启动端口或提交密钥', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => Response.json({ ...snapshot, fields: [
      ...snapshot.fields,
      { name: 'port', description: '监听端口', kind: 'number', value: 8080,
        nullable: false, secret_is_set: false, restart_required: true,
        scope: 'static', read_only: true },
      { name: 'onebot_v11_access_token', description: 'OneBot 密钥', kind: 'secret', value: null,
        nullable: false, secret_is_set: true, restart_required: true,
        scope: 'static', read_only: true },
    ] })));
    render(<Settings token="offline-token" />);
    await screen.findByLabelText('温度');
    fireEvent.click(screen.getByRole('tab', { name: '静态配置' }));
    expect(await screen.findByLabelText('监听端口')).toBeDisabled();
    expect(screen.getByLabelText('OneBot 密钥')).toBeDisabled();
    expect(screen.getByRole('button', { name: '保存修改' })).toBeDisabled();
    expect(screen.queryByLabelText('温度')).not.toBeInTheDocument();
  });

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
      values: { chat_llm_temperature: 0.4 } }));
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
