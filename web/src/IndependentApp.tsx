import { Alert, Button, ConfigProvider, Input } from 'antd';
import { useEffect, useState } from 'react';
import { Autonomy } from './pages/Autonomy';
import { errorMessage } from './api';

/** 独立Agent入口只装配自主对话，不加载QQ状态、插件列表或机器人控制面板。 */
export function IndependentApp() {
  const [mode, setMode] = useState<boolean>();
  const [token, setToken] = useState('');
  const [draft, setDraft] = useState('');
  const [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    fetch('/api/v1/auth', { signal: controller.signal }).then(response => {
      if (!response.ok) throw new Error('读取登录模式失败');
      return response.json();
    }).then(value => setMode(value.password_required === true)).catch(failure => {
      if (!controller.signal.aborted) setError(errorMessage(failure));
    });
    return () => controller.abort();
  }, []);
  /** 认证成功后才挂载Agent数据视图，密码仅保存在本页面内存。 */
  async function login() {
    setError('');
    try {
      const response = await fetch('/api/v1/autonomy/profiles?limit=1', { headers: { Authorization: `Bearer ${draft}` } });
      if (!response.ok) throw new Error('管理密码无效');
      setToken(draft); setDraft(''); setMode(false);
    } catch (failure) { setError(errorMessage(failure)); }
  }
  return <ConfigProvider><main className="independent-agent">
    <header className="independent-heading"><strong>Independent Agent</strong><span>身份 · 对话 · 记忆 · 自主循环</span></header>
    {error && <Alert type="error" message={error} />}
    {mode === undefined ? <p>正在连接独立Agent…</p> : mode ? <form onSubmit={event => { event.preventDefault(); void login(); }}>
      <label htmlFor="independent-password">管理密码</label><Input.Password id="independent-password" value={draft} onChange={event => setDraft(event.target.value)} />
      <Button htmlType="submit" type="primary">进入Agent</Button>
    </form> : <Autonomy token={token} />}
  </main></ConfigProvider>;
}
