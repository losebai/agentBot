import { Input } from 'antd';
import { useEffect, useRef, useState } from 'react';
import { errorMessage } from '../api';

// undefined 表示保留，null 表示管理员明确清除；已保存明文只在编辑弹窗内驻留。
export function CredentialInput({label, value, onChange, editing, configured, available, required = false, onReveal}: {
  label: string; value?: string|null; onChange: (value: string|null|undefined)=>void;
  editing: boolean; configured: boolean; available: boolean; required?: boolean;
  onReveal?: () => Promise<string>;
}) {
  const cleared = value === null && !required;
  const [visible, setVisible] = useState(false);
  const [stored, setStored] = useState<string>();
  const [revealing, setRevealing] = useState(false);
  const [revealError, setRevealError] = useState('');
  const generation = useRef(0);
  const reveal = useRef(onReveal);
  reveal.current = onReveal;

  /** 打开编辑弹窗即加载已有值并保持隐藏；卸载后过期结果不能重新写回。 */
  useEffect(() => {
    if (!editing || !configured || !reveal.current) return;
    const request = ++generation.current;
    setRevealing(true); setRevealError('');
    void reveal.current().then(credential => {
      if (generation.current === request) setStored(credential);
    }).catch(failure => {
      if (generation.current === request) setRevealError('密钥读取失败：' + errorMessage(failure));
    }).finally(() => {
      if (generation.current === request) setRevealing(false);
    });
    return () => { generation.current += 1; };
  }, [editing, configured]);

  /** 显示和隐藏只切换输入类型，关闭弹窗时组件卸载并释放已读取明文。 */
  async function changeVisibility(next: boolean) {
    if (!next) {
      setVisible(false); setRevealError('');
      return;
    }
    if (value || stored || !configured || !reveal.current) { setVisible(true); return; }
    const request = ++generation.current;
    setRevealing(true); setRevealError('');
    try {
      const credential = await reveal.current();
      if (generation.current === request) { setStored(credential); setVisible(true); }
    } catch (failure) {
      if (generation.current === request) setRevealError('密钥读取失败：' + errorMessage(failure));
    } finally {
      if (generation.current === request) setRevealing(false);
    }
  }

  return <>
    <label>{label}<Input.Password maxLength={4096} autoComplete="new-password"
      disabled={cleared || revealing} required={required && (!configured || value === null)}
      value={value===null?'':value??stored??''} visibilityToggle={{ visible, onVisibleChange: next => void changeVisibility(next) }}
      placeholder={editing&&configured?'已设置，留空保持':required?'请输入密钥':'无需鉴权时留空'}
      onChange={event=>onChange(event.target.value||undefined)}/></label>
    {revealError&&<p role="alert">{revealError}</p>}
    <p>{configured?'已加载保存的凭据，点击眼睛可显示或隐藏；关闭后清除页面明文。':required?'必须设置凭据。':available?'无需鉴权时可以留空。':'需要重新设置凭据。'}凭据保存到数据库。</p>
    {editing&&!required&&<label className="checkbox-label"><input type="checkbox" checked={value===null}
      onChange={event=>onChange(event.target.checked?null:undefined)}/>清除{label}并改为无需鉴权</label>}
  </>;
}
