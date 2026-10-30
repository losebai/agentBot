import { CheckCircleFilled, ExportOutlined, QrcodeOutlined, ReloadOutlined } from '@ant-design/icons';
import { Alert, Button, QRCode, Space, Spin, Tag } from 'antd';
import { useEffect, useRef, useState } from 'react';
import { ApiClient, errorMessage } from '../api';
import type { NapCatLogin } from '../types';
import { napcatErrors } from './NapCatStatus';

type PluginState = Awaited<ReturnType<ApiClient['napcatPlugin']>>;
const labels = { not_started: '未启动', starting: '启动中', running: '运行中',
  stopped: '已停止', failed: '启动失败', plugin_disabled: 'QQ 插件未启用' };
const loginLabels = { generating: '正在生成二维码', waiting: '使用手机 QQ 扫码登录',
  scanned: '已扫码，请在手机上确认登录', connecting: '正在连接 QQ', online: 'QQ 已登录',
  error: '登录未完成，请刷新二维码后重试' };

/** 本页直接扫码；后台由桌面交给默认浏览器，网页不接触后台管理密钥。 */
export function NapCatPlugin({ api }: { api: ApiClient }) {
  const [state, setState] = useState<PluginState>();
  const [login, setLogin] = useState<NapCatLogin>();
  const [error, setError] = useState('');
  const [action, setAction] = useState('');
  const active = useRef(false);
  const working = useRef(false);
  const reading = useRef<AbortController | null>(null);
  const generation = useRef(0);
  const loginFailed = useRef(false);
  const port = useRef<number | null | undefined>(null);

  useEffect(() => {
    active.current = true;
    const currentGeneration = ++generation.current;
    working.current = false;
    loginFailed.current = false;
    setAction(''); setLogin(undefined);
    let timer: ReturnType<typeof setTimeout>;
    /** 串行读取当前实例；失败后等待用户重试，避免后台认证限流。 */
    async function refresh() {
      if (!working.current) {
        const controller = new AbortController();
        reading.current = controller;
        const valid = () => active.current && generation.current === currentGeneration && !controller.signal.aborted;
        let received = false;
        try {
          const value = await api.napcatPlugin(controller.signal);
          if (valid()) {
            received = true;
            setState(value);
            if (port.current !== value.webui_port || !value.status.webui_ready) {
              port.current = value.webui_port;
              loginFailed.current = false;
              setLogin(undefined); setError('');
            }
            if (value.available && value.plugin_enabled && value.status.webui_ready && !loginFailed.current) {
              const result = await api.napcatLogin(controller.signal);
              if (valid()) { setLogin(result); setError(''); }
            }
          }
        } catch (failure) {
          if (valid()) {
            loginFailed.current = true;
            if (!received) setState(undefined);
            setLogin(undefined); setError(errorMessage(failure));
          }
        }
      }
      if (active.current && generation.current === currentGeneration) timer = setTimeout(() => void refresh(), 2000);
    }
    void refresh();
    return () => { active.current = false; ++generation.current; clearTimeout(timer); reading.current?.abort(); };
  }, [api]);

  /** 操作先取消读取；卸载或重启后不再显示旧二维码和旧端口的结果。 */
  async function operate(kind: 'start' | 'refresh' | 'admin') {
    if (working.current) return;
    working.current = true;
    const currentGeneration = generation.current;
    reading.current?.abort();
    const controller = new AbortController();
    reading.current = controller;
    setAction(kind); setError('');
    if (kind !== 'admin') setLogin(undefined);
    const valid = () => active.current && generation.current === currentGeneration && !controller.signal.aborted;
    try {
      if (kind === 'start') {
        await api.startNapcat();
        const value = await api.napcatPlugin(controller.signal);
        if (valid()) { setState(value); loginFailed.current = false; }
      } else if (kind === 'refresh') {
        const value = await api.refreshNapcatLogin(controller.signal);
        if (valid()) { setLogin(value); loginFailed.current = false; }
      } else await api.openNapcatAdmin();
    } catch (failure) {
      if (valid()) {
        setError(errorMessage(failure));
        if (kind !== 'admin') loginFailed.current = true;
      }
    } finally {
      if (valid()) { working.current = false; setAction(''); }
    }
  }

  const enabled = Boolean(state?.available && state.plugin_enabled);
  const ready = enabled && Boolean(state?.status.webui_ready);
  const running = state?.status.state === 'starting' || state?.status.state === 'running';
  return <section className="setting-row napcat-access" aria-label="QQ NapCat 接入">
    <div className="setting-description">
      <div className="napcat-heading"><h3>NapCat 接入</h3>
        {state?.status && <Tag color={ready ? 'green' : undefined}>{labels[state.status.state]}</Tag>}
      </div>
      {!state && !error && <Spin size="small" />}
      {state && !state.available && <p>请在 Windows 桌面版中启动 NapCat。</p>}
      {state?.available && !state.plugin_enabled && <p>请先启用 QQ 插件并重启后端。</p>}
      {state?.available && <>
        <p>启动后直接扫码，完成手机确认即可接入。</p>
        <p className="napcat-port">管理端口 <code>{ready && state.webui_port ? state.webui_port : '未监听'}</code></p>
      </>}
      <Space wrap className="napcat-actions">
        <Button type="primary" disabled={!enabled || running || Boolean(action)} loading={action === 'start'}
          onClick={() => void operate('start')}>启动 NapCat</Button>
        <Button icon={<ExportOutlined aria-hidden />} disabled={!ready || Boolean(action)} loading={action === 'admin'}
          onClick={() => void operate('admin')}>打开后台管理界面</Button>
      </Space>
      {state?.status.error_code && <Alert type="error" showIcon message={
        napcatErrors[state.status.error_code] || 'NapCat 启动未完成，请检查运行环境。'
      } />}
      {error && <Alert type="error" showIcon message={error} />}
    </div>
    {state?.available && <div className="napcat-login" aria-label="QQ 扫码登录">
      <div className="napcat-qr">
        {login?.status === 'online' ? <CheckCircleFilled className="napcat-online" />
          : login?.qr_code_url ? <div role="img" aria-label="QQ 登录二维码">
            <QRCode value={login.qr_code_url} type="svg" size={184} bordered={false} />
          </div> : ready && !error ? <Spin /> : <QrcodeOutlined className="napcat-placeholder" />}
      </div>
      <strong>{login ? loginLabels[login.status] : ready ? '等待登录二维码' : '启动后显示登录二维码'}</strong>
      {login?.status !== 'online' && <Button type="text" icon={<ReloadOutlined aria-hidden />} disabled={!ready || Boolean(action)}
        loading={action === 'refresh'} onClick={() => void operate('refresh')}>刷新二维码</Button>}
    </div>}
  </section>;
}
