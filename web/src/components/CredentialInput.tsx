// 密钥不回填；undefined 表示保留，null 表示管理员明确清除。
export function CredentialInput({label, value, onChange, editing, configured, available}: {
  label: string; value?: string|null; onChange: (value: string|null|undefined)=>void;
  editing: boolean; configured: boolean; available: boolean;
}) {
  return <>
    <label>{label}<input type="password" maxLength={4096} autoComplete="new-password"
      disabled={value===null} value={value??''}
      placeholder={editing&&configured?'已设置，留空保持':'无需鉴权时留空'}
      onChange={event=>onChange(event.target.value||undefined)}/></label>
    <p>{configured?'凭据已设置，读取接口不会返回原值。':available?'无需鉴权时可以留空。':'需要重新设置凭据。'}凭据保存到数据库。</p>
    {editing&&<label className="checkbox-label"><input type="checkbox" checked={value===null}
      onChange={event=>onChange(event.target.checked?null:undefined)}/>清除{label}并改为无需鉴权</label>}
  </>;
}
