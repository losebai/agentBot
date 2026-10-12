import { Alert, Button, Spin, Tag } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { useEffect, useMemo, useState } from 'react';
import { ApiClient, errorMessage } from '../api';
import type { BotInfo, ServiceStatus } from '../types';

/** 展示各能力实际后端和进程媒体存储状态。 */
export function Overview({ token }: { token: string }) {
  const api = useMemo(() => new ApiClient(token), [token]);
  const [status, setStatus] = useState<ServiceStatus>();
  const [bots, setBots] = useState<BotInfo[]>([]);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  /** 同时刷新运行时状态和机器人连接。 */
  async function refresh() {
    setLoading(true); setError('');
    try { const [next, connections] = await Promise.all([api.status(), api.bots()]); setStatus(next); setBots(connections); }
    catch (failure) { setError(errorMessage(failure)); }
    finally { setLoading(false); }
  }
  useEffect(() => { void refresh(); }, [api]);
  return <div className="page-content">
    <div className="page-heading"><div><p className="eyebrow">WORKSPACE OVERVIEW</p><h1>运行概览</h1><p>查看机器人连接与配置应用状态。</p></div><Button icon={<ReloadOutlined />} loading={loading} onClick={() => void refresh()}>刷新状态</Button></div>
    {error && <Alert message={error} type="error" showIcon className="notice" />}
    {loading && !status ? <div className="loading"><Spin /></div> : status && <>
      <div className="metrics-grid">
        <div className="metric"><span>服务状态</span><strong className={status.ready ? 'healthy' : 'waiting'}>{status.ready ? '就绪' : '依赖异常'}</strong><p>数据库与持久队列</p></div>
        <div className="metric"><span>在线机器人</span><strong>{status.bot_count}<small> 个</small></strong><p>当前 OneBot 连接</p></div>
        <div className="metric"><span>保存版本</span><strong>{status.database_revision}</strong><p>数据库配置</p></div>
        <div className="metric"><span>应用版本</span><strong>{status.applied_revision}</strong><p>当前运行时</p></div>
      </div>
      {status.restart_required_fields.length > 0 && <Alert className="notice" showIcon type="warning" message={`以下设置需要重启：${status.restart_required_fields.join(', ')}`} />}
      <section className="panel chat-scope" style={{padding:'18px 22px',marginBottom:24,flexWrap:'wrap'}}><strong>AI 能力</strong><Tag color="cyan">普通对话：{status.plain_ai_backend==='python'?'Python':'Java'}</Tag><Tag color="cyan">Agent：{status.agent_backend==='python'?'Python':'Java'}</Tag><Tag color="cyan">媒体：{status.media_backend==='python'?'Python':'Java'}</Tag>{status.python_media&&<span>媒体存储：{status.python_media.code||(status.python_media.storage_available?'已就绪':'未就绪')}</span>}{status.python_ai && <span>Python 配置：{status.python_ai.code || (status.python_ai.initialized?'已初始化':'未初始化')} · 模型 {status.configured_model}</span>}{status.python_agent && <span>Agent 目录：{status.python_agent.code || (status.python_agent.initialized?'已初始化':'未初始化')} · 预设 {status.configured_agent || '使用聊天配置'}</span>}<span>视频、Laya、通用任务和代码执行继续使用 Java。</span></section>
      <div className="overview-grid"><section className="panel"><div className="panel-header"><h2>机器人连接</h2><Tag>{bots.length} 个在线</Tag></div>
        {!bots.length ? <div className="empty-connections"><div className="connection-symbol">○</div><h3>等待机器人连接</h3><p>在 NapCat 中配置反向 WebSocket，连接此服务的<br /><code>/onebot/v11/ws</code>。</p></div>
          : bots.map(bot => <div className="bot-row" key={bot.id}><div className="bot-avatar">AB</div><div><strong>{bot.id}</strong><p>{bot.adapter}</p></div><Tag color="green">在线</Tag></div>)}</section>
        <section className="panel"><div className="panel-header"><h2>工作台说明</h2></div><div className="info-list"><div><span className="info-dot" /><div><h3>配置集中管理</h3><p>修改后查看保存与应用版本，确认是否需要重启。</p></div></div><div><span className="info-dot" /><div><h3>对话独立测试</h3><p>先验证模型回复，再接入 QQ 群聊或私聊。</p></div></div><div><span className="info-dot" /><div><h3>持续运行</h3><p>关闭浏览器不会停止机器人服务。</p></div></div></div></section></div>
    </>}
  </div>;
}
