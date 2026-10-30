import { Alert, Tag } from 'antd';
import type { NapCatStatus as NapCatObservation } from '../types';

/** 只展示固定错误提示，不把本机安装路径或启动参数传入页面。 */
export const napcatErrors: Record<string, string> = {
  PAYLOAD_INVALID: 'NapCat 运行载荷缺失或校验失败，请安装包含 Node 运行包的新版桌面程序。',
  CONFIG_INVALID: 'NapCat 配置无法读取或保存，请检查文件格式和权限。',
  CONFIG_CONFLICT: '托管模式与正向 OneBot 地址冲突，请清理接入地址后手动重启。',
  INSTANCE_BUSY: '托管目录已被另一个实例使用，或目录无法访问。',
  PORT_UNAVAILABLE: 'NapCat 登录页端口被占用或无法监听。',
  START_FAILED: 'NapCat 启动失败，请检查运行环境。',
  PROCESS_EXITED: 'NapCat 已意外退出，请通过桌面服务菜单重启。',
  WEBUI_UNAVAILABLE: '所属 NapCat 登录页未就绪或已停止，请检查端口配置。',
  PLATFORM_UNSUPPORTED: 'NapCat 托管仅支持 Windows 桌面版。',
};

/** 进程运行与 QQ 在线分别判断，网页只展示状态，不提供本机进程控制。 */
export function NapCatStatus({ status, botCount, error }: {
  status?: NapCatObservation; botCount: number; error?: string;
}) {
  if (error) return <Alert type="warning" showIcon message={`NapCat 状态读取失败：${error}`} />;
  if (!status || status.mode === 'external') return null;
  const labels = { not_started: '未启动', starting: '启动中', running: '运行中',
    stopped: '已停止', failed: '异常', plugin_disabled: 'QQ 插件未启用' };
  // 进程存活不代表完成 QQ 登录，在线状态以适配器连接为准。
  return <section className="panel" aria-label="NapCat 托管状态">
    <div className="panel-header"><h2>NapCat 托管</h2><Tag>{labels[status.state]}</Tag></div>
    {status.error_code && <Alert type="error" showIcon message={napcatErrors[status.error_code] || 'NapCat 托管异常，请查看插件状态。'} />}
    {status.state === 'running' && <p>{botCount > 0 ? 'QQ 已在线' : '等待登录或连接'}</p>}
    {status.webui_ready && <p>登录服务已就绪，可在“QQ 配置”直接扫码，或使用浏览器打开后台管理界面。</p>}
    <p>模式、端口或 Token 保存后，通过“服务 → 重启后端”生效。</p>
  </section>;
}
