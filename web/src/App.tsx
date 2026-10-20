import { Alert, Button, ConfigProvider, Input, Spin } from 'antd';
import { DashboardOutlined, LogoutOutlined, MessageOutlined, SettingOutlined, ApiOutlined, RobotOutlined, HistoryOutlined, TagsOutlined } from '@ant-design/icons';
import { useEffect, useState } from 'react';
import { ApiClient, errorMessage } from './api';
import { Overview } from './pages/Overview';
import { Settings } from './pages/Settings';
import { Chat } from './pages/Chat';
import { Providers } from './pages/Providers';
import { Sessions } from './pages/Sessions';
import { AgentManagement } from './pages/AgentManagement';
import { Plugins } from './pages/Plugins';
import { Knowledge } from './pages/Knowledge';

// 组件库与页面共享白粉蓝主题，统一控件尺寸、可读性和状态反馈。
const workspaceTheme = {
  token: {
    colorPrimary: '#6975d3', colorInfo: '#2563eb', colorSuccess: '#24836b',
    colorWarning: '#9b702f', colorError: '#b94b4b',
    colorSuccessBg: '#f5f8f5', colorSuccessBorder: '#dce7de',
    colorWarningBg: '#fcf9f1', colorWarningBorder: '#ece1c8',
    colorErrorBg: '#fcf5f5', colorErrorBorder: '#eddcdc',
    colorInfoBg: '#eff6ff', colorInfoBorder: '#cddffc',
    colorText: '#1e293b', colorTextSecondary: '#64748b',
    colorBorder: '#d8e1ed', colorBgLayout: '#f3f6fb',
    borderRadius: 8, fontSize: 14, controlHeight: 38,
    fontFamily: 'Inter, Segoe UI, Microsoft YaHei, sans-serif',
  },
  components: {
    Button: { primaryShadow: 'none', defaultShadow: 'none', dangerShadow: 'none' },
    Tabs: { inkBarColor: '#8c78cf', itemSelectedColor: '#6975d3', itemHoverColor: '#9e65b0' },
    Select: { optionSelectedBg: '#eef0ff', optionActiveBg: '#f7f3fc' },
    Modal: { borderRadiusLG: 14 },
  },
};

const titles: Record<string, string> = {overview:'运行概览', settings:'机器人配置', plugins:'插件管理', chat:'对话测试', providers:'供应商与模型', sessions:'会话记录', agents:'Agent 管理', knowledge:'标签与知识库'};

/** 按服务端登录模式进入工作台；知识与记忆使用独立入口，离开时取消页面请求。 */
export function App() {
  // null 表示尚未登录，空字符串表示后端明确启用了免登录。
  const [token, setToken] = useState<string | null>(null);
  const [access, setAccess] = useState<'loading' | 'ready' | 'failed'>('loading');
  const [candidate, setCandidate] = useState('');
  const [error, setError] = useState('');
  const [connecting, setConnecting] = useState(false);
  const [page, setPage] = useState('overview');
  const [visited, setVisited] = useState(['overview']);
  useEffect(() => {
    const controller = new AbortController();
    /** 先确认是否需要密码；查询失败时保留错误，不能自动放行。 */
    async function checkAccess() {
      try {
        const mode = await new ApiClient('').auth(controller.signal);
        if (controller.signal.aborted) return;
        if (mode.password_required === false) setToken('');
        setAccess('ready');
      } catch (failure) {
        if (controller.signal.aborted) return;
        setError(errorMessage(failure));
        setAccess('failed');
      }
    }
    void checkAccess();
    // 卸载或 StrictMode 重建时取消旧查询，防止旧响应改变入口状态。
    return () => controller.abort();
  }, []);
  /** 先验证密码，成功后才把密码放入本次页面内存。 */
  async function connect() {
    if (!candidate.trim() || connecting) return;
    setConnecting(true); setError('');
    try { await new ApiClient(candidate).status(); setToken(candidate); setCandidate(''); }
    catch (failure) { setError(errorMessage(failure)); }
    finally { setConnecting(false); }
  }
  function navigate(next: string) { setPage(next); setVisited(previous => [...new Set([...previous, next])]); }
  return <ConfigProvider theme={workspaceTheme}>
    {access !== 'ready' ? <main className="page-content">{access === 'loading' ? <Spin tip="正在连接管理服务"><div className="loading" /></Spin> : <Alert message={error} type="error" showIcon />}</main>
      : token === null ? <main className="login-screen">
      <form className="login-form" onSubmit={event => { event.preventDefault(); void connect(); }}>
        <div className="brand-lockup"><span className="bot-mark">AB</span><span>AgentBot</span></div>
        <h1>登录工作台</h1><p>输入管理员密码，管理你的机器人。</p>
        {error && <Alert message={error} type="error" showIcon className="notice" />}
        <label htmlFor="admin-password">管理员密码</label>
        <Input.Password id="admin-password" value={candidate} autoComplete="off" size="large" placeholder="输入管理员密码" onChange={event => setCandidate(event.target.value)} />
        <Button block type="primary" size="large" htmlType="submit" loading={connecting} disabled={!candidate.trim()}>登录</Button>
        <span className="login-note">密码只保留在本次浏览器会话中。</span>
      </form></main>
      : <div className="workspace"><aside className="sidebar"><div className="brand-lockup"><span className="bot-mark">AB</span><span>AgentBot</span></div><p className="nav-label">工作空间</p><nav aria-label="主导航">{[
        { key: 'overview', label: '运行概览', icon: <DashboardOutlined /> },
        { key: 'settings', label: '机器人配置', icon: <SettingOutlined /> },
        { key: 'plugins', label: '插件管理', icon: <ApiOutlined /> },
        { key: 'chat', label: '对话测试', icon: <MessageOutlined /> },
        { key: 'providers', label: '供应商与模型', icon: <ApiOutlined /> },
        { key: 'sessions', label: '会话记录', icon: <HistoryOutlined /> },
        { key: 'knowledge', label: '标签与知识库', icon: <TagsOutlined /> },
        { key: 'agents', label: 'Agent 管理', icon: <RobotOutlined /> },
      ].map(item => <button key={item.key} className={page === item.key ? 'active' : ''} aria-current={page === item.key ? 'page' : undefined} onClick={() => navigate(item.key)}>{item.icon}{item.label}</button>)}</nav><div className="sidebar-footer"><span className="status-dot" />管理服务已连接{token && <button onClick={() => { setToken(null); setVisited(['overview']); setPage('overview'); }} aria-label="退出管理"><LogoutOutlined /></button>}</div></aside>
        <main className="main-area"><header className="topbar"><span className="topbar-title">{titles[page]}</span><span className="workspace-label">AgentBot</span></header>
          <section hidden={page !== 'overview'}>{visited.includes('overview') && <Overview token={token} />}</section>
          <section hidden={page !== 'settings'}>{visited.includes('settings') && <Settings token={token} onPlugins={() => navigate('plugins')} />}</section>
          <section hidden={page !== 'plugins'}>{visited.includes('plugins') && <Plugins token={token} />}</section>
          <section hidden={page !== 'chat'}>{page === 'chat' && <Chat token={token} />}</section>
          <section hidden={page !== 'providers'}>{visited.includes('providers') && <Providers token={token} />}</section>
          <section hidden={page !== 'sessions'}>{page === 'sessions' && <Sessions token={token} />}</section>
          <section hidden={page !== 'knowledge'}>{page === 'knowledge' && <Knowledge token={token} />}</section>
          <section hidden={page !== 'agents'}>{visited.includes('agents') && <AgentManagement token={token} />}</section>
        </main></div>}
  </ConfigProvider>;
}
