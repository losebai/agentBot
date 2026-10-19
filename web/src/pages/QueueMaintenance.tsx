import { Alert, Button, Card, Space } from 'antd';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ApiClient, errorMessage } from '../api';
import type { DeadLetterStatus } from '../types';

/** 独立执行队列维护，避免清除操作提交、刷新或丢弃设置页的配置草稿。 */
export function QueueMaintenance({ token }: { token: string }) {
  const api = useMemo(() => new ApiClient(token), [token]);
  const controller = useRef<AbortController>();
  const inFlight = useRef<AbortSignal>();
  const [status, setStatus] = useState<DeadLetterStatus>();
  const [loading, setLoading] = useState(true);
  const [clearing, setClearing] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  /** 刷新只读取队列计数，失败后禁止使用旧计数执行清除，允许手动重试。 */
  const reload = useCallback(async (signal: AbortSignal) => {
    if (inFlight.current) return;
    inFlight.current = signal;
    setLoading(true); setError('');
    try {
      const result = await api.deadLetters(signal);
      if (!signal.aborted) setStatus(result);
    } catch (failure) {
      if (!signal.aborted) { setStatus(undefined); setError(errorMessage(failure)); }
    } finally {
      // 旧请求取消后可能晚于新请求结束，不能释放新请求持有的锁。
      if (inFlight.current === signal) inFlight.current = undefined;
      if (!signal.aborted) setLoading(false);
    }
  }, [api]);

  useEffect(() => {
    const current = new AbortController();
    controller.current = current;
    setClearing(false); setNotice('');
    void reload(current.signal);
    // 切换分类或离开页面后取消等待，服务端已完成的清除以下次读取结果为准。
    return () => {
      current.abort();
      if (inFlight.current === current.signal) inFlight.current = undefined;
    };
  }, [reload]);

  /** 一次点击直接清除失败记录；同步锁防止 React 更新按钮前重复发出请求。 */
  async function clear() {
    const signal = controller.current?.signal;
    if (!signal || signal.aborted || inFlight.current || !status?.available || !status.total) return;
    inFlight.current = signal;
    setClearing(true); setError(''); setNotice('');
    try {
      const result = await api.clearDeadLetters(signal);
      if (signal.aborted) return;
      setNotice(`已清除 ${result.cleared} 条死信。`);
      // 成功表示两类队列均已清空；旧计数可能已被先前部分清除或其他进程改变。
      // 本次实际删除量只用于提示，并发新增记录通过独立刷新重新查询。
      setStatus({ available: true, text: 0, images: 0, total: 0 });
    } catch (failure) {
      if (!signal.aborted) setError(errorMessage(failure));
    } finally {
      if (inFlight.current === signal) inFlight.current = undefined;
      if (!signal.aborted) setClearing(false);
    }
  }

  return <section aria-label="队列维护" className="notice">
    <Card title="队列维护" size="small">
      <p>仅删除失败的队列记录，保留待处理任务、图片文件、聊天历史与记忆。</p>
      {loading ? <p role="status">正在读取死信数量…</p> : status && <Space wrap>
        <span>文本死信：{status.text} 条</span>
        <span>图片死信：{status.images} 条</span>
      </Space>}
      {!loading && status?.available === false && <p>QQ 插件未启用或未运行，队列维护暂不可用。</p>}
      {error && <Alert message={error} type="error" showIcon className="notice" />}
      {notice && <Alert message={notice} type="success" showIcon className="notice" />}
      <div className="heading-actions">
        <Button disabled={loading || clearing} onClick={() => {
          if (controller.current) void reload(controller.current.signal);
        }}>刷新死信数量</Button>
        <Button danger loading={clearing} disabled={loading || clearing || !status?.available || !status.total}
          onClick={() => void clear()}>一键清除死信</Button>
      </div>
    </Card>
  </section>;
}
