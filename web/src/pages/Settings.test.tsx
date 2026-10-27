import { useRunningPluginFixture } from '../test-plugins';
import { chooseSelect, selectDisplay } from '../test-select';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { Settings } from './Settings';

useRunningPluginFixture();

const snapshot = {
  database_revision: 1, applied_revision: 1, restart_required_fields: [], startup_revision: 'startup-file-one',
  fields: [
    { name: 'chat_llm_temperature', description: '温度', kind: 'number', value: 0.7,
      nullable: true, secret_is_set: false, restart_required: false },
    { name: 'chat_naraka_default_role_id', description: '默认角色 ID', kind: 'secret', value: null,
      nullable: true, secret_is_set: true, restart_required: false },
  ],
};

describe('配置编辑', () => {
  it('自动发言补充规则可留空并提示两种触发的消息来源', async () => {
    let submitted: unknown;
    const fields = [{ name: 'chat_auto_speak_prompt', description: '自动发言补充规则',
      kind: 'string', value: '原有规则', nullable: false, secret_is_set: false,
      restart_required: false }];
    vi.stubGlobal('fetch', vi.fn(async (_url, init) => {
      if (init?.method === 'PATCH') submitted = JSON.parse(init.body);
      return Response.json({ ...snapshot, fields });
    }));
    render(<Settings token="offline-token" />);
    fireEvent.click(await screen.findByRole('button', { name: '自动回复设置' }));
    const prompt = await screen.findByLabelText('自动发言补充规则');
    expect(prompt.tagName).toBe('TEXTAREA');
    expect(screen.getByText(/message 使用触发时消息，interval 使用群里最新有效消息/)).toBeInTheDocument();
    fireEvent.change(prompt, { target: { value: '' } });
    fireEvent.click(screen.getByRole('button', { name: '保存修改' }));
    await waitFor(() => expect(submitted).toEqual({ expected_revision: 1,
      values: { chat_auto_speak_prompt: '' } }));
  });

  it('自动回复分类可编辑和保存多条回复规则', async () => {
    let submitted: unknown;
    const fields = [
      { name: 'chat_multi_reply_enabled', description: '自动多条回复', kind: 'boolean', value: false },
      { name: 'chat_multi_reply_max_messages', description: '最多消息数', kind: 'number', value: 3 },
      { name: 'chat_multi_reply_interval_seconds', description: '消息间隔', kind: 'number', value: 1 },
      { name: 'chat_multi_reply_prompt', description: '分条提示词', kind: 'string', value: '按语义分条' },
    ].map(field => ({ ...field, nullable: false, secret_is_set: false, restart_required: false }));
    vi.stubGlobal('fetch', vi.fn(async (_url, init) => {
      if (init?.method === 'PATCH') submitted = JSON.parse(init.body);
      return Response.json({ ...snapshot, fields });
    }));
    render(<Settings token="offline-token" />);
    fireEvent.click(await screen.findByRole('button', { name: '自动回复设置' }));
    fireEvent.click(await screen.findByLabelText('自动多条回复'));
    fireEvent.change(screen.getByLabelText('最多消息数'), { target: { value: '4' } });
    fireEvent.change(screen.getByLabelText('消息间隔'), { target: { value: '0.5' } });
    const prompt = screen.getByLabelText('分条提示词');
    expect(prompt.tagName).toBe('TEXTAREA');
    fireEvent.change(prompt, { target: { value: '完整意思一条\n短回答只发一条' } });
    fireEvent.click(screen.getByRole('button', { name: '保存修改' }));
    await waitFor(() => expect(submitted).toEqual({ expected_revision: 1, values: {
      chat_multi_reply_enabled: true, chat_multi_reply_max_messages: 4,
      chat_multi_reply_interval_seconds: 0.5, chat_multi_reply_prompt: '完整意思一条\n短回答只发一条',
    } }));
  });

  it.each(['string', 'json'])('对象配置类型为 %s 时显示格式化 JSON，保存对象而非字符串', async kind => {
    let submitted: unknown;
    const replies = { '早上好': '早安！', '晚安': '好梦' };
    const fields = [{ name: 'chat_keyword_at_replies', description: '关键词回复规则', kind,
      value: replies, nullable: false, secret_is_set: false, restart_required: false }];
    vi.stubGlobal('fetch', vi.fn(async (_url, init) => {
      if (init?.method === 'PATCH') submitted = JSON.parse(init.body);
      return Response.json({ ...snapshot, fields });
    }));
    render(<Settings token="offline-token" />);
    fireEvent.click(await screen.findByRole('button', { name: '自动回复设置' }));
    const control = await screen.findByLabelText('关键词回复规则');
    expect(control.tagName).toBe('TEXTAREA');
    expect(control).toHaveValue(JSON.stringify(replies, null, 2));
    expect(control).not.toHaveValue('[object Object]');
    fireEvent.change(control, { target: { value: '{"你好":"你好呀！"}' } });
    fireEvent.click(await screen.findByRole('button', { name: 'QQ' }));
    fireEvent.click(await screen.findByRole('button', { name: '自动回复设置' }));
    expect(screen.getByLabelText('关键词回复规则')).toHaveValue('{"你好":"你好呀！"}');
    fireEvent.click(screen.getByRole('button', { name: '保存修改' }));
    await waitFor(() => expect(submitted).toEqual({ expected_revision: 1,
      values: { chat_keyword_at_replies: { '你好': '你好呀！' } } }));
  });

  it('旧元数据把数组标为数值时仍显示 JSON，非法 JSON 保留草稿且不发送请求', async () => {
    let submitted = false;
    const fields = [{ name: 'chat_auto_speak_groups', description: '自动发言群', kind: 'number',
      value: ['123456', '789012'], nullable: false, secret_is_set: false, restart_required: false }];
    vi.stubGlobal('fetch', vi.fn(async (_url, init) => {
      if (init?.method === 'PATCH') submitted = true;
      return Response.json({ ...snapshot, fields });
    }));
    render(<Settings token="offline-token" />);
    fireEvent.click(await screen.findByRole('button', { name: '自动回复设置' }));
    const control = await screen.findByLabelText('自动发言群');
    expect(control.tagName).toBe('TEXTAREA');
    expect(control).toHaveValue(JSON.stringify(fields[0].value, null, 2));
    fireEvent.change(control, { target: { value: '[not JSON' } });
    fireEvent.click(screen.getByRole('button', { name: '保存修改' }));
    await screen.findByRole('alert');
    expect(submitted).toBe(false);
    expect(control).toHaveValue('[not JSON');
  });

  it('机器人名称按行编辑并以名称列表保存，清空后关闭名字触发', async () => {
    let submitted: unknown;
    const fields = [{name: 'nickname', description: '机器人名称与触发别名', kind: 'json',
      value: ['桃桃', '小桃'], nullable: false, secret_is_set: false, restart_required: false,
      scope: 'runtime', read_only: false}];
    vi.stubGlobal('fetch', vi.fn(async (_url, init) => {
      if (init?.method === 'PATCH') submitted = JSON.parse(init.body);
      return Response.json({...snapshot, fields});
    }));
    render(<Settings token="offline-token" />);
    fireEvent.click(await screen.findByRole('button', { name: 'QQ' }));
    const names = await screen.findByLabelText('机器人名称与触发别名');
    expect(names).toBeEnabled();
    expect(names).toHaveValue('桃桃\n小桃');
    fireEvent.change(names, {target: {value: ' 新名字 \n\n 新别名 '}});
    fireEvent.click(screen.getByRole('button', {name: '保存修改'}));
    await waitFor(() => expect(submitted).toEqual({expected_revision: 1,
      values: {nickname: ['新名字', '新别名']}}));
    await waitFor(() => expect(screen.getByRole('button', {name: '保存修改'})).toBeDisabled());
    fireEvent.change(names, {target: {value: ''}});
    fireEvent.click(screen.getByRole('button', {name: '保存修改'}));
    await waitFor(() => expect(submitted).toEqual({expected_revision: 1, values: {nickname: []}}));
  });

  it.each([null, false, true])('可空布尔值 %s 能保留原值并显式清除为 null', async initial => {
    let submitted: unknown;
    const fields = [{ name: 'chat_llm_enable_thinking', description: '深度思考', kind: 'boolean',
      value: initial, nullable: true, secret_is_set: false, restart_required: false }];
    vi.stubGlobal('fetch', vi.fn(async (_url, init) => {
      if (init?.method === 'PATCH') {
        submitted = JSON.parse(init.body);
        fields[0].value = JSON.parse(init.body).values.chat_llm_enable_thinking;
      }
      return Response.json({ ...snapshot, fields });
    }));
    render(<Settings token="offline-token" />);
    fireEvent.click(screen.getByText('LLM设置', { selector: 'button' }));
    const control = await screen.findByLabelText('深度思考');
    expect(selectDisplay(control)).toHaveTextContent(initial === null ? '未设置（不传此参数）' : initial ? '启用' : '禁用');
    const selected = initial === false;
    await chooseSelect(control, selected ? '启用' : '禁用');
    fireEvent.click(screen.getByText('保存修改').closest('button')!);
    await waitFor(() => expect(submitted).toEqual({ expected_revision: 1,
      values: { chat_llm_enable_thinking: selected } }));
    await waitFor(() => expect(screen.getByText('保存修改').closest('button')!).toBeDisabled());
    await chooseSelect(control, '未设置（不传此参数）');
    fireEvent.click(screen.getByText('保存修改').closest('button')!);
    await waitFor(() => expect(submitted).toEqual({ expected_revision: 1,
      values: { chat_llm_enable_thinking: null } }));
  });

  it('基础设置中的静态端口和会话密钥只读并提示重启', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => Response.json({ ...snapshot, fields: [
      ...snapshot.fields,
      { name: 'port', description: '监听端口', kind: 'number', value: 8080,
        nullable: false, secret_is_set: false, restart_required: true,
        scope: 'static', read_only: true },
      { name: 'chat_session_secret', description: '会话密钥', kind: 'secret', value: null,
        nullable: false, secret_is_set: true, restart_required: true,
        scope: 'static', read_only: true },
    ] })));
    render(<Settings token="offline-token" />);
    expect(await screen.findByLabelText('监听端口')).toBeDisabled();
    expect(screen.getByLabelText('会话密钥')).toBeDisabled();
    expect(screen.getByText(/启动配置保存后/)).toBeInTheDocument();
    expect(screen.getAllByText('重启生效')).toHaveLength(2);
    expect(screen.getByRole('button', { name: '保存修改' })).toBeDisabled();
    expect(screen.queryByLabelText('温度')).not.toBeInTheDocument();
  });

  it('环境变量覆盖时 OneBot Token 只读脱敏，不生成页面草稿', async () => {
    const fields = [
      { name: 'onebot_v11_access_token', description: 'OneBot Token', kind: 'secret', value: null,
        nullable: false, secret_is_set: true, restart_required: true,
        scope: 'static', read_only: true },
      { name: 'chat_session_secret', description: '会话密钥', kind: 'secret', value: null,
        nullable: false, secret_is_set: true, restart_required: true,
        scope: 'static', read_only: true },
    ];
    vi.stubGlobal('fetch', vi.fn(async () => Response.json({ ...snapshot, fields })));
    render(<Settings token="offline-token" />);
    fireEvent.click(await screen.findByRole('button', { name: 'QQ' }));
    const control = await screen.findByLabelText('OneBot Token');
    expect(control).toBeDisabled();
    expect(control).toHaveValue('');
    expect(screen.getByText(/当前字段由启动环境覆盖/)).toHaveTextContent('与 NapCat Token 保持一致');
    expect(screen.getByText('重启生效')).toBeInTheDocument();
    expect(screen.queryByLabelText('会话密钥')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '保存修改' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '基础设置' }));
    expect(await screen.findByLabelText('会话密钥')).toBeDisabled();
  });

  it('静态端口、Token 与动态草稿一起提交版本，静态密钥留空保留原值', async () => {
    let submitted: unknown;
    const fields = [...snapshot.fields,
      { name: 'onebot_v11_access_token', description: 'OneBot Token', kind: 'secret', value: null,
        nullable: false, secret_is_set: true, restart_required: true,
        scope: 'static', read_only: false },
      { name: 'chat_session_secret', description: '会话密钥', kind: 'secret', value: null,
        nullable: false, secret_is_set: true, restart_required: true,
        scope: 'static', read_only: false },
      { name: 'chat_db_password', description: '数据库密码', kind: 'secret', value: null,
        nullable: false, secret_is_set: true, restart_required: true,
        scope: 'static', read_only: false },
      { name: 'port', description: '监听端口', kind: 'number', value: 8080,
        nullable: false, secret_is_set: false, restart_required: true,
        scope: 'static', read_only: false }];
    vi.stubGlobal('fetch', vi.fn(async (_url, init) => {
      if (init?.method === 'PATCH') {
        submitted = JSON.parse(init.body);
        return Response.json({ ...snapshot, fields, database_revision: 2,
          startup_revision: 'startup-file-two', restart_required_fields: ['onebot_v11_access_token', 'port'] });
      }
      return Response.json({ ...snapshot, fields });
    }));
    render(<Settings token="offline-token" />);
    const session = await screen.findByLabelText('会话密钥');
    expect(session).toBeEnabled();
    fireEvent.change(session, { target: { value: 'temporary-session-secret' } });
    fireEvent.change(session, { target: { value: '' } });
    fireEvent.change(screen.getByLabelText('数据库密码'), { target: { value: 'temporary-database-password' } });
    fireEvent.change(screen.getByLabelText('数据库密码'), { target: { value: '' } });
    fireEvent.change(screen.getByLabelText('监听端口'), { target: { value: '9090' } });
    fireEvent.click(screen.getByRole('button', { name: 'LLM设置' }));
    fireEvent.change(screen.getByLabelText('温度'), { target: { value: '0.4' } });
    fireEvent.click(await screen.findByRole('button', { name: 'QQ' }));
    const control = screen.getByLabelText('OneBot Token');
    expect(control).toBeEnabled();
    expect(control).toHaveValue('');
    expect(screen.getByText(/静态启动配置/)).toHaveTextContent('服务→重启后端');
    fireEvent.change(control, { target: { value: 'new-static-token-at-least-thirty-two-characters' } });
    fireEvent.click(screen.getByRole('button', { name: '保存修改' }));
    await waitFor(() => expect(submitted).toEqual({ expected_revision: 1, startup_revision: 'startup-file-one',
      values: { chat_llm_temperature: 0.4, onebot_v11_access_token: 'new-static-token-at-least-thirty-two-characters', port: 9090 } }));
    expect(await screen.findByText('需要重启：onebot_v11_access_token, port')).toBeInTheDocument();
    expect(control).toHaveValue('');
  });

  it('仅修改静态端口也提交启动文件版本，冲突保留草稿', async () => {
    let submitted: unknown;
    const fields = [{ name: 'port', description: '监听端口', kind: 'number', value: 8080,
      nullable: false, secret_is_set: false, restart_required: true, scope: 'static', read_only: false }];
    vi.stubGlobal('fetch', vi.fn(async (_url, init) => {
      if (init?.method === 'PATCH') {
        submitted = JSON.parse(init.body);
        return Response.json({ code: 'STARTUP_CONFLICT', message: '启动文件已被修改，请重新加载' }, { status: 409 });
      }
      return Response.json({ ...snapshot, fields });
    }));
    render(<Settings token="offline-token" />);
    fireEvent.change(await screen.findByLabelText('监听端口'), { target: { value: '9090' } });
    fireEvent.click(screen.getByRole('button', { name: '保存修改' }));
    expect(await screen.findByText('启动文件已被修改，请重新加载')).toBeInTheDocument();
    expect(screen.getByLabelText('监听端口')).toHaveValue(9090);
    expect(submitted).toEqual({ expected_revision: 1, startup_revision: 'startup-file-one', values: { port: 9090 } });
  });

  it('可空静态密钥手动留空保留，明确清除才提交 null 和启动文件版本', async () => {
    let submitted: unknown;
    const fields = [{ name: 'onebot_v11_secret', description: 'OneBot 签名密钥', kind: 'secret', value: null,
      nullable: true, secret_is_set: true, restart_required: true, scope: 'static', read_only: false }];
    vi.stubGlobal('fetch', vi.fn(async (_url, init) => {
      if (init?.method === 'PATCH') submitted = JSON.parse(init.body);
      return Response.json({ ...snapshot, fields });
    }));
    render(<Settings token="offline-token" />);
    fireEvent.click(await screen.findByRole('button', { name: 'QQ' }));
    const control = await screen.findByLabelText('OneBot 签名密钥');
    expect(control).toBeEnabled();
    fireEvent.change(control, { target: { value: 'temporary-signature-secret' } });
    fireEvent.change(control, { target: { value: '' } });
    expect(screen.getByRole('button', { name: '保存修改' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '清除此密钥' }));
    expect(control).toHaveValue('');
    expect(screen.getByText(/已标记清除此密钥/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '保存修改' }));
    await waitFor(() => expect(submitted).toEqual({ expected_revision: 1,
      startup_revision: 'startup-file-one', values: { onebot_v11_secret: null } }));
  });

  it('清空可编辑静态 Token 后移除该草稿，保存其他修改不发送启动文件版本', async () => {
    let submitted: unknown;
    const fields = [...snapshot.fields,
      { name: 'onebot_v11_access_token', description: 'OneBot Token', kind: 'secret', value: null,
        nullable: false, secret_is_set: true, restart_required: true,
        scope: 'static', read_only: false }];
    vi.stubGlobal('fetch', vi.fn(async (_url, init) => {
      if (init?.method === 'PATCH') submitted = JSON.parse(init.body);
      return Response.json({ ...snapshot, fields });
    }));
    render(<Settings token="offline-token" />);
    fireEvent.click(await screen.findByRole('button', { name: 'QQ' }));
    const control = await screen.findByLabelText('OneBot Token');
    expect(control).toBeEnabled();
    fireEvent.change(control, { target: { value: 'temporary-static-token' } });
    expect(screen.getByRole('button', { name: '保存修改' })).toBeEnabled();
    fireEvent.change(control, { target: { value: '' } });
    expect(screen.getByRole('button', { name: '保存修改' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'LLM设置' }));
    fireEvent.change(screen.getByLabelText('温度'), { target: { value: '0.4' } });
    fireEvent.click(await screen.findByRole('button', { name: 'QQ' }));
    fireEvent.change(screen.getByLabelText('OneBot Token'), { target: { value: 'temporary-static-token' } });
    fireEvent.change(screen.getByLabelText('OneBot Token'), { target: { value: '' } });
    fireEvent.click(screen.getByRole('button', { name: '保存修改' }));
    await waitFor(() => expect(submitted).toEqual({ expected_revision: 1, values: { chat_llm_temperature: 0.4 } }));
  });

  it('静态 Token 不创建或提交网页草稿，跨分类保存保留其他修改', async () => {
    let submitted: unknown;
    const fields = [...snapshot.fields,
      { name: 'onebot_v11_access_token', description: 'OneBot Token', kind: 'secret', value: null,
        nullable: false, secret_is_set: true, restart_required: true,
        scope: 'static', read_only: true }];
    vi.stubGlobal('fetch', vi.fn(async (_url, init) => {
      if (init?.method === 'PATCH') submitted = JSON.parse(init.body);
      return Response.json({ ...snapshot, fields });
    }));
    render(<Settings token="offline-token" />);
    fireEvent.click(await screen.findByRole('button', { name: 'QQ' }));
    const control = await screen.findByLabelText('OneBot Token');
    expect(control).toBeDisabled();
    // 即使人为触发表单事件，只读权限也不能生成 Token 草稿。
    fireEvent.change(control, { target: { value: 'temporary-token' } });
    fireEvent.change(control, { target: { value: '' } });
    expect(screen.getByRole('button', { name: '保存修改' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'LLM设置' }));
    fireEvent.change(await screen.findByLabelText('温度'), { target: { value: '0.4' } });
    fireEvent.click(await screen.findByRole('button', { name: 'QQ' }));
    expect(screen.getByLabelText('OneBot Token')).toHaveValue('');
    // Token 的只读事件不影响已经编辑的其他分类草稿。
    fireEvent.change(screen.getByLabelText('OneBot Token'), { target: { value: 'temporary-token' } });
    fireEvent.change(screen.getByLabelText('OneBot Token'), { target: { value: '' } });
    expect(screen.getByRole('button', { name: '保存修改' })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: '保存修改' }));
    await waitFor(() => expect(submitted).toEqual({ expected_revision: 1,
      values: { chat_llm_temperature: 0.4 } }));
  });

  it('QQ 接入、自动回复与游戏资讯独立展示，搜索只筛选当前分类', async () => {
    const fields = [
      { name: 'chat_max_sessions', description: '最大会话数', kind: 'number', value: 1000 },
      { name: 'future_setting', description: '新配置', kind: 'string', value: '默认基础' },
      { name: 'chat_python_agent_code', description: 'Agent 预设', kind: 'string', value: 'main' },
      { name: 'chat_memory_enabled', description: '会话记忆', kind: 'boolean', value: true },
      { name: 'chat_vision_model', description: '图片理解模型', kind: 'string', value: 'vision' },
      { name: 'chat_image_model', description: '图片生成模型名称', kind: 'string', value: 'gpt-image-1' },
      { name: 'chat_image_recognition_enabled', description: '图片识别', kind: 'boolean', value: true },
      { name: 'chat_mention_tool_enabled', description: '群成员 AT 工具', kind: 'boolean', value: true },
      { name: 'chat_allowed_groups', description: '群访问列表', kind: 'json', value: [] },
      { name: 'chat_auto_speak_enabled', description: '自动发言', kind: 'boolean', value: false },
      { name: 'chat_keyword_at_replies', description: '关键词自动回复', kind: 'json', value: {} },
      { name: 'chat_cooldown_seconds', description: '聊天冷却', kind: 'number', value: 1 },
      { name: 'chat_group_recent_context_enabled', description: '群近期上下文', kind: 'boolean', value: false },
      { name: 'chat_naraka_cookie', description: '永劫账号 Cookie', kind: 'secret', value: null },
      { name: 'chat_naraka_cooldown_seconds', description: '永劫查询冷却', kind: 'number', value: 5 },
      { name: 'command_start', description: '命令前缀', kind: 'json', value: ['/'],
        scope: 'static', read_only: true, restart_required: true },
      { name: 'chat_local_workflow_base_url', description: 'Comfy 服务地址', kind: 'string', value: 'http://127.0.0.1:8188' },
      { name: 'chat_image_generation_enabled', description: '图片生成', kind: 'boolean', value: true },
    ].map(field => ({ nullable: false, secret_is_set: false, restart_required: false,
      scope: 'runtime', read_only: false, ...field }));
    vi.stubGlobal('fetch', vi.fn(async url => Response.json(String(url).includes('/ai/models?')
      ? { items: [{provider_name: '测试供应商', reference: `model:${1}`, provider_id: 1, upstream_model: 'vision', revision: 1,  id: 1, alias: 'vision', enabled: true },
        {provider_name: '测试供应商', reference: `model:${2}`, provider_id: 1, upstream_model: 'gpt-image-1', revision: 1,  id: 2, alias: 'gpt-image-1', enabled: true }], next_cursor: null }
      : { ...snapshot, fields })));
    render(<Settings token="offline-token" />);
    const navigation = screen.getByRole('navigation', { name: '机器人配置分类' });
    await within(navigation).findByRole('button', { name: 'QQ' });
    expect(within(navigation).getAllByRole('button').map(button => button.textContent)).toEqual([
      '基础设置', 'LLM设置', 'QQ', '自动回复设置', '游戏资讯设置', 'Comfy',
    ]);
    expect(await screen.findByLabelText('最大会话数')).toBeInTheDocument();
    expect(screen.getByLabelText('新配置')).toBeInTheDocument();
    fireEvent.click(within(navigation).getByRole('button', { name: 'LLM设置' }));
    // 生图模型由模型供应商提供，应与其他模型一起配置。
    for (const label of ['Agent 预设', '会话记忆', '图片理解模型', '图片生成模型名称', '图片识别', '图片生成']) {
      expect(screen.getByLabelText(label)).toBeInTheDocument();
    }
    fireEvent.click(within(navigation).getByRole('button', { name: 'QQ' }));
    for (const label of ['群成员 AT 工具', '群访问列表']) {
      expect(screen.getByLabelText(label)).toBeEnabled();
    }
    expect(screen.getByLabelText('命令前缀')).toBeDisabled();
    expect(screen.queryByLabelText('自动发言')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('永劫账号 Cookie')).not.toBeInTheDocument();
    fireEvent.click(within(navigation).getByRole('button', { name: '自动回复设置' }));
    for (const label of ['自动发言', '关键词自动回复', '聊天冷却', '群近期上下文']) {
      expect(screen.getByLabelText(label)).toBeEnabled();
    }
    expect(screen.queryByLabelText('群访问列表')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('永劫查询冷却')).not.toBeInTheDocument();
    fireEvent.click(within(navigation).getByRole('button', { name: '游戏资讯设置' }));
    expect(screen.getByLabelText('永劫账号 Cookie')).toHaveValue('');
    expect(screen.getByLabelText('永劫查询冷却')).toBeEnabled();
    expect(screen.queryByLabelText('聊天冷却')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('自动发言')).not.toBeInTheDocument();
    fireEvent.click(within(navigation).getByRole('button', { name: 'Comfy' }));
    expect(screen.getByLabelText('Comfy 服务地址')).toBeInTheDocument();
    expect(screen.queryByLabelText('图片生成')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('图片生成模型名称')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('图片识别')).not.toBeInTheDocument();
    fireEvent.change(screen.getByRole('textbox', { name: '搜索配置' }), { target: { value: '图片' } });
    expect(screen.queryByLabelText('图片生成')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('图片理解模型')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Comfy 服务地址')).not.toBeInTheDocument();
  });

  it('跨分类保留草稿并一起提交可写字段', async () => {
    let submitted: unknown;
    const fields = [...snapshot.fields,
      { name: 'chat_auto_speak_interval_seconds', description: '自动发言间隔', kind: 'number', value: 3600,
        nullable: false, secret_is_set: false, restart_required: false,
        scope: 'runtime', read_only: false },
      { name: 'nickname', description: '机器人名称', kind: 'json', value: ['桃桃'],
        nullable: false, secret_is_set: false, restart_required: false,
        scope: 'runtime', read_only: false }];
    vi.stubGlobal('fetch', vi.fn(async (_url, init) => {
      if (init?.method === 'PATCH') submitted = JSON.parse(init.body);
      return Response.json({ ...snapshot, fields });
    }));
    render(<Settings token="offline-token" />);
    fireEvent.click(screen.getByText('LLM设置', { selector: 'button' }));
    fireEvent.change(await screen.findByLabelText('温度'), { target: { value: '0.4' } });
    fireEvent.click(screen.getByText('QQ', { selector: 'button' }));
    fireEvent.change(screen.getByLabelText('机器人名称'), { target: { value: '小桃' } });
    fireEvent.click(screen.getByText('自动回复设置', { selector: 'button' }));
    fireEvent.change(screen.getByLabelText('自动发言间隔'), { target: { value: '1800' } });
    fireEvent.click(screen.getByText('游戏资讯设置', { selector: 'button' }));
    fireEvent.change(screen.getByLabelText('默认角色 ID'), { target: { value: 'game-role' } });
    fireEvent.click(screen.getByText('LLM设置', { selector: 'button' }));
    expect(screen.getByLabelText('温度')).toHaveValue(0.4);
    fireEvent.click(screen.getByText('保存修改').closest('button')!);
    await waitFor(() => expect(submitted).toEqual({ expected_revision: 1,
      values: { chat_llm_temperature: 0.4, nickname: ['小桃'],
        chat_auto_speak_interval_seconds: 1800, chat_naraka_default_role_id: 'game-role' } }));
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
    fireEvent.click(screen.getByRole('button', { name: 'LLM设置' }));
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
    fireEvent.click(screen.getByRole('button', { name: 'LLM设置' }));
    fireEvent.change(await screen.findByLabelText('温度'), { target: { value: '0.4' } });
    fireEvent.click(screen.getByRole('button', { name: '保存修改' }));
    expect(await screen.findByText('配置已被修改，请重新加载')).toBeInTheDocument();
    expect(screen.getByLabelText('温度')).toHaveValue(0.4);
  });
});
