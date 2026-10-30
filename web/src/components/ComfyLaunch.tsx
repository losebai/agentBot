import { Alert, Button, Spin, Tag } from 'antd';
import { useEffect, useRef, useState } from 'react';
import { ApiClient, errorMessage } from '../api';
import type { ComfyStatus } from '../types';

const labels = { not_configured: '未配置', offline: '未启动', starting: '启动中', online: '在线', failed: '启动失败' };
const errors: Record<string, string> = {
  configuration_missing: '请配置 Python 解释器和 ComfyUI 目录，保存后手动重启后端。',
  invalid_configuration: '启动配置无效，请检查 Python、main.py 和附加参数。',
  unsupported_url: '本机启动只支持本机 HTTP 根地址；其他地址请使用已启动的外部服务。',
  port_in_use: '服务端口已被其他程序占用，请检查 Comfy 服务地址与端口。',
  launch_failed: 'ComfyUI 启动失败，请检查 Python 环境、依赖及本机日志。',
  process_exited: 'ComfyUI 进程提前退出，请检查 Python 依赖和本机日志。',
  startup_timeout: 'ComfyUI 在 120 秒内未就绪，本次启动的进程已回收，请检查本机日志。',
  restart_required: '服务地址已改变，请手动重启后端后再启动 ComfyUI。',
  closed: '启动监督器已关闭，请手动重启后端。',
};

/** 独立显示 ComfyUI 服务状态，不将插件运行当作服务在线。 */
export function ComfyLaunch({ api }: { api: ApiClient }) {
  const [state, setState] = useState<ComfyStatus>();
  const [error, setError] = useState('');
  const [starting, setStarting] = useState(false);
  const generation = useRef(0);
  const working = useRef(false);
  const reading = useRef<AbortController | null>(null);

  useEffect(() => {
    const current = ++generation.current;
    working.current = false;
    setStarting(false); setState(undefined); setError('');
    let timer: ReturnType<typeof setTimeout>;
    /** 每次请求结束再安排下一次读取，写请求期间暂停，避免并发及旧状态覆盖。 */
    async function refresh() {
      if (!working.current) {
        const controller = new AbortController();
        reading.current = controller;
        try {
          const value = await api.comfyStatus(controller.signal);
          if (generation.current === current && !controller.signal.aborted) {
            setState(value); setError('');
          }
        } catch (failure) {
          if (generation.current === current && !controller.signal.aborted) setError(errorMessage(failure));
        }
      }
      if (generation.current === current) timer = setTimeout(() => void refresh(), 2000);
    }
    void refresh();
    return () => { ++generation.current; clearTimeout(timer); reading.current?.abort(); };
  }, [api]);

  /** 离开只放弃操作响应，启动意图一旦提交仍由后端独立完成。 */
  async function start() {
    if (working.current) return;
    working.current = true;
    const current = generation.current;
    reading.current?.abort();
    setStarting(true); setError('');
    try {
      const value = await api.startComfy();
      if (generation.current === current) setState(value);
    } catch (failure) {
      if (generation.current === current) setError(errorMessage(failure));
    } finally {
      if (generation.current === current) { working.current = false; setStarting(false); }
    }
  }

  return <section className="setting-row" aria-label="ComfyUI 服务启动">
    <div className="setting-description"><h3>ComfyUI 服务</h3>
      {state && <Tag>{labels[state.state]}</Tag>}
      {!state && !error && <Spin size="small" />}
      <p>手动启动条件：Comfy 插件运行、本机同源管理页面、本机 HTTP 服务地址、有效的 Python 和 ComfyUI 目录、已安装依赖且端口空闲。</p>
      <p>模型、显存和工作流影响生成任务，不作为服务启动条件。</p>
      <p>当前生效的启动配置（保存配置后需手动重启后端生效）：</p>
      <p>Python：{state?.python || '未设置'}；ComfyUI 目录：{state?.directory || '未设置'}</p>
      <p>启动配置：{state?.configured ? '已配置（附加参数不回显）' : '尚未完成'}</p>
      {state?.state === 'online' && !state.managed && <p>正在使用已在线的外部实例。</p>}
      {state && !state.local && <p>当前服务地址不支持本机托管启动，可连接外部服务。</p>}
      {state?.error_code && <Alert type="error" showIcon message={errors[state.error_code]
        || 'ComfyUI 启动未完成，请检查有效配置与本机日志。'} />}
      {error && <Alert type="error" showIcon message={error} />}
    </div>
    <div className="setting-control"><Button loading={starting}
      disabled={!state?.configured || !state.local || state.state === 'online' || state.state === 'starting' || starting}
      onClick={() => void start()}>启动 ComfyUI</Button></div>
  </section>;
}
