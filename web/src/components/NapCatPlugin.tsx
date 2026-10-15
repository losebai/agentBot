import { Alert, Button, Space, Spin, Tag } from 'antd';
import { useEffect, useRef, useState } from 'react';
import { ApiClient, errorMessage } from '../api';
import { napcatErrors } from './NapCatStatus';

type PluginState = Awaited<ReturnType<ApiClient['napcatPlugin']>>;
const labels = { not_started: '未启动', starting: '启动中', running: '运行中',
  stopped: '已停止', failed: '启动失败', plugin_disabled: 'QQ 插件未启用' };

/** QQ 插件内只提供所属 NapCat 的启动和登录，状态读取失败不影响插件开关。 */
export function NapCatPlugin({ api }: { api: ApiClient }) {
  const [state, setState] = useState<PluginState>();
  const [error, setError] = useState('');
  const [action, setAction] = useState('');
  const active = useRef(false);
  const working = useRef(false);
  const reading = useRef<AbortController | null>(null);
  const generation = useRef(0);

  useEffect(() => {
    active.current = true;
    const currentGeneration = ++generation.current;
    working.current = false;
    setAction('');
    let timer: ReturnType<typeof setTimeout>;
    /** 串行刷新，跳过操作期间的读取，避免旧状态覆盖刚提交的启动结果。 */
    async function refresh() {
      if (!working.current) {
        const controller = new AbortController();
        reading.current = controller;
        try {
          const value = await api.napcatPlugin(controller.signal);
          if (active.current && generation.current === currentGeneration && !controller.signal.aborted) setState(value);
        } catch (failure) {
          if (active.current && generation.current === currentGeneration && !controller.signal.aborted) setError(errorMessage(failure));
        }
      }
      if (active.current && generation.current === currentGeneration) timer = setTimeout(() => void refresh(), 2000);
    }
    void refresh();
    return () => { active.current = false; ++generation.current; clearTimeout(timer); reading.current?.abort(); };
  }, [api]);

  /** 写请求提交后由后端完成；卸载只放弃显示，不假装撤销已经开始的进程操作。 */
  async function operate(kind: 'start' | 'login') {
    if (working.current) return;
    working.current = true;
    const currentGeneration = generation.current;
    reading.current?.abort();
    setAction(kind); setError('');
    try {
      if (kind === 'start') await api.startNapcat();
      else await api.openNapcatLogin();
      const value = await api.napcatPlugin();
      if (active.current && generation.current === currentGeneration) setState(value);
    } catch (failure) {
      if (active.current && generation.current === currentGeneration) setError(errorMessage(failure));
    } finally {
      if (active.current && generation.current === currentGeneration) {
        working.current = false;
        setAction('');
      }
    }
  }

  const enabled = Boolean(state?.available && state.plugin_enabled);
  const running = state?.status?.state === 'starting' || state?.status?.state === 'running';
  return <section className="setting-row" aria-label="QQ NapCat 接入">
    <div className="setting-description"><h3>NapCat 接入</h3>
      {state?.status && <Tag>{labels[state.status.state]}</Tag>}
      {!state && !error && <Spin size="small" />}
      {state && !state.available && <p>请在 Windows 桌面版中启动 NapCat。</p>}
      {state?.available && !state.plugin_enabled && <p>请先启用 QQ 插件并重启后端。</p>}
      {state?.available && <p>启动后打开登录页完成 QQ 登录；进程运行不代表 QQ 已在线。</p>}
      {state?.status?.error_code && <Alert type="error" showIcon message={
        napcatErrors[state.status.error_code] || 'NapCat 启动未完成，请检查运行环境。'
      } />}
      {error && <Alert type="error" showIcon message={error} />}
    </div>
    <Space className="setting-control">
      <Button disabled={!enabled || running || Boolean(action)} loading={action === 'start'}
        onClick={() => void operate('start')}>启动 NapCat</Button>
      <Button disabled={!enabled || !state?.status?.webui_ready || Boolean(action)} loading={action === 'login'}
        onClick={() => void operate('login')}>打开登录页</Button>
    </Space>
  </section>;
}
