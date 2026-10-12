import { Alert, Button, ConfigProvider, Input } from 'antd';
import { DashboardOutlined, LogoutOutlined, MessageOutlined, SettingOutlined, SafetyCertificateOutlined } from '@ant-design/icons';
import { useState } from 'react';
import { ApiClient, errorMessage } from './api';
import { Overview } from './pages/Overview';
import { Settings } from './pages/Settings';
import { Chat } from './pages/Chat';
import { Providers } from './pages/Providers';
import { Models } from './pages/Models';
import { Sessions } from './pages/Sessions';
import { Agents } from './pages/Agents';
import { Skills } from './pages/Skills';
import { McpServers } from './pages/McpServers';

const titles: Record<string, string> = {overview:'运行概览', settings:'机器人配置', chat:'对话测试', providers:'供应商管理', models:'模型管理', sessions:'会话记录', agents:'Agent 管理', skills:'Skill 管理', mcps:'MCP 服务'};

/** 管理凭据只保留在内存；离开对话与记录页面时卸载活动媒体资源。 */
export function App() {
  const [token, setToken] = useState('');
  const [candidate, setCandidate] = useState('');
  const [error, setError] = useState('');
  const [connecting, setConnecting] = useState(false);
  const [page, setPage] = useState('overview');
  const [visited, setVisited] = useState(['overview']);
  /** 先认证状态接口，成功后才把凭据放入本次页面内存。 */
  async function connect() {
    if (!candidate.trim() || connecting) return;
    setConnecting(true); setError('');
    try { await new ApiClient(candidate.trim()).status(); setToken(candidate.trim()); setCandidate(''); }
    catch (failure) { setError(errorMessage(failure)); }
    finally { setConnecting(false); }
  }
  function navigate(next: string) { setPage(next); setVisited(previous => [...new Set([...previous, next])]); }
  return <ConfigProvider theme={{ token: { colorPrimary: '#16766c', borderRadius: 8, fontFamily: 'Inter, Segoe UI, Microsoft YaHei, sans-serif' } }}>
    {!token ? <main className="login-screen"><div className="login-identity"><div className="brand-lockup"><span className="bot-mark">AB</span><span>AgentBot</span></div><h1>你的机器人，<br />一个工作台。</h1><p>配置、连接与对话测试，集中管理。</p><div className="login-decoration"><i /><i /><i /></div></div>
      <form className="login-form" onSubmit={event => { event.preventDefault(); void connect(); }}><SafetyCertificateOutlined className="login-icon" /><p className="eyebrow">MANAGEMENT ACCESS</p><h2>连接管理服务</h2><p>输入服务启动时设置的管理员凭据。</p>{error && <Alert message={error} type="error" showIcon className="notice" />}<label htmlFor="admin-token">管理员凭据</label><Input.Password id="admin-token" value={candidate} autoComplete="off" size="large" placeholder="输入管理员凭据" onChange={event => setCandidate(event.target.value)} /><Button block type="primary" size="large" htmlType="submit" loading={connecting} disabled={!candidate.trim()}>连接服务</Button><span className="login-note">凭据只保留在本次浏览器会话中。</span></form></main>
      : <div className="workspace"><aside className="sidebar"><div className="brand-lockup"><span className="bot-mark">AB</span><span>AgentBot<small>机器人工作台</small></span></div><p className="nav-label">工作空间</p><nav aria-label="主导航">{[
        { key: 'overview', label: '运行概览', icon: <DashboardOutlined /> },
        { key: 'settings', label: '机器人配置', icon: <SettingOutlined /> },
        { key: 'chat', label: '对话测试', icon: <MessageOutlined /> },
        { key: 'providers', label: '供应商管理', icon: <SettingOutlined /> },
        { key: 'models', label: '模型管理', icon: <DashboardOutlined /> },
        { key: 'sessions', label: '会话记录', icon: <MessageOutlined /> },
        { key: 'agents', label: 'Agent 管理', icon: <DashboardOutlined /> },
        { key: 'skills', label: 'Skill 管理', icon: <SettingOutlined /> },
        { key: 'mcps', label: 'MCP 服务', icon: <SettingOutlined /> },
      ].map(item => <button key={item.key} className={page === item.key ? 'active' : ''} aria-current={page === item.key ? 'page' : undefined} onClick={() => navigate(item.key)}>{item.icon}{item.label}</button>)}</nav><div className="sidebar-footer"><span className="status-dot" />管理服务已连接<button onClick={() => { setToken(''); setVisited(['overview']); setPage('overview'); }} aria-label="退出管理"><LogoutOutlined /></button></div></aside>
        <main className="main-area"><header className="topbar"><span>工作空间 / {titles[page]}</span><span className="workspace-label">AgentBot · 第四阶段</span></header>
          <section hidden={page !== 'overview'}>{visited.includes('overview') && <Overview token={token} />}</section>
          <section hidden={page !== 'settings'}>{visited.includes('settings') && <Settings token={token} />}</section>
          <section hidden={page !== 'chat'}>{page === 'chat' && <Chat token={token} />}</section>
          <section hidden={page !== 'providers'}>{visited.includes('providers') && <Providers token={token} />}</section>
          <section hidden={page !== 'models'}>{visited.includes('models') && <Models token={token} />}</section>
          <section hidden={page !== 'sessions'}>{page === 'sessions' && <Sessions token={token} />}</section>
          <section hidden={page !== 'agents'}>{visited.includes('agents') && <Agents token={token} />}</section>
          <section hidden={page !== 'skills'}>{visited.includes('skills') && <Skills token={token} />}</section>
          <section hidden={page !== 'mcps'}>{visited.includes('mcps') && <McpServers token={token} />}</section>
        </main></div>}
  </ConfigProvider>;
}
