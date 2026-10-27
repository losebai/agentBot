import { Alert, Button, QRCode, Space, Tag } from 'antd';
import { useEffect, useRef, useState } from 'react';
import { ApiClient, errorMessage } from '../api';

export interface BilibiliStatus {
  logged_in: boolean; status: string; uid?: string; name?: string; checked_at?: number;
  credential_set?: boolean;
}
export interface BilibiliQr {
  session_id: string; url: string; expires_at: number;
  status: 'waiting' | 'scanned' | 'confirmed' | 'expired';
}
const labels = { waiting: '等待扫码', scanned: '请在手机上确认登录', confirmed: '登录成功', expired: '二维码已过期' };

/** 仅当前运行中的插件可登录；替换二维码、清除账号和卸载都会废止旧页面请求。 */
export function BilibiliPlugin({ api, enabled }: { api: ApiClient; enabled: boolean }) {
  const [status, setStatus] = useState<BilibiliStatus>();
  const [qr, setQr] = useState<BilibiliQr>();
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const generation = useRef(0);
  const reading = useRef<AbortController>();
  const timer = useRef<ReturnType<typeof setTimeout>>();

  /** 每次操作废止之前的读取，后端已提交的写操作不伪装为可撤销。 */
  function begin() {
    reading.current?.abort(); clearTimeout(timer.current);
    reading.current = new AbortController();
    return { id: ++generation.current, signal: reading.current.signal };
  }

  useEffect(() => {
    if (enabled) void operate('status');
    return () => { ++generation.current; reading.current?.abort(); clearTimeout(timer.current); };
  }, [api, enabled]);

  /** 串行轮询当前会话，结束后刷新账号；失败停止并由用户显式重试。 */
  async function poll(value: BilibiliQr, id: number, signal: AbortSignal) {
    try {
      const result = await api.bilibiliPollQr(value.session_id, signal);
      if (generation.current !== id || signal.aborted) return;
      const next = { ...value, status: result.status };
      setQr(next);
      if (result.status === 'confirmed') {
        const account = await api.bilibiliStatus(signal);
        if (generation.current === id && !signal.aborted) { setStatus(account); setQr(undefined); }
      } else if (result.status !== 'expired') {
        timer.current = setTimeout(() => void poll(next, id, signal), 2000);
      }
    } catch (failure) {
      if (generation.current === id && !signal.aborted) setError(errorMessage(failure));
    }
  }

  /** 账号写操作只能由用户点击；初次读取仅检查登录。 */
  async function operate(kind: 'status' | 'qr' | 'clear') {
    const { id, signal } = begin(); setBusy(true); setError(''); setQr(undefined);
    try {
      if (kind === 'qr') {
        const value = await api.bilibiliGenerateQr(signal);
        if (generation.current !== id || signal.aborted) return;
        setQr(value); void poll(value, id, signal);
      } else {
        if (kind === 'clear') await api.bilibiliClearLogin();
        const account = await api.bilibiliStatus(signal);
        if (generation.current === id && !signal.aborted) setStatus(account);
      }
    } catch (failure) {
      if (generation.current === id && !signal.aborted) setError(errorMessage(failure));
    } finally {
      if (generation.current === id && !signal.aborted) setBusy(false);
    }
  }

  return <section className="setting-row" aria-label="B站账号">
    <div className="setting-description"><h3>B站账号</h3>
      {!enabled ? <p>请先启用 B站插件并重启后端。</p> : <>
        {status && <Tag color={status.logged_in ? 'green' : undefined}>
          {status.logged_in ? '已登录' : status.status === 'expired' ? '登录已失效' : '未登录'}</Tag>}
        {status?.logged_in && <p>{status.name} · UID {status.uid}</p>}
        {qr && <div><Tag>{labels[qr.status]}</Tag>
          {qr.status !== 'expired' && <QRCode value={qr.url} type="svg" size={180} />}
          <p>使用哔哩哔哩手机客户端扫码，并在手机确认登录。</p></div>}
        <p>评论由 Agent 按需读取和回复；投稿成功后仍需等待平台审核。</p>
      </>}
      {error && <Alert type="error" showIcon message={error} />}
    </div>
    <Space wrap className="setting-control">
      <Button disabled={!enabled || busy} onClick={() => void operate('qr')}>{qr ? '重新生成二维码' : '扫码登录'}</Button>
      <Button disabled={!enabled || busy} onClick={() => void operate('status')}>检查登录</Button>
      <Button disabled={!enabled || busy || (!status?.credential_set && !status?.logged_in && !qr)} onClick={() => void operate('clear')}>清除本地登录</Button>
    </Space>
  </section>;
}
