import { Alert } from 'antd';
import { ModelSelect } from './ModelSelect';
import { useModelDirectory } from './useModelDirectory';

/** 三种角色仅展示启用模型并允许复用，停用的已有绑定保持原值供管理员明确替换。 */
export function AgentModelSelect({ token, value, onChange, visionValue, fallbackValue,
  onVisionChange, onFallbackChange }: {
  token: string; value: number; onChange: (value: number) => void;
  visionValue?: number | null; fallbackValue?: number | null;
  onVisionChange?: (value: number | null) => void;
  onFallbackChange?: (value: number | null) => void;
}) {
  const { page, busy, error, load } = useModelDirectory(token);

  const disabledIds = new Set(page.items.filter(model => !model.enabled).map(model => model.id));
  const selectedRoutes = [
    { label: onVisionChange ? '聊天模型' : '默认模型', id: value },
    ...(onVisionChange ? [{ label: '图片理解模型', id: visionValue }] : []),
    ...(onFallbackChange ? [{ label: '错误转发模型', id: fallbackValue }] : []),
  ];

  return <>
    {error && <Alert type="error" showIcon message={'模型列表读取失败：' + error} />}
    <fieldset><legend>模型路由</legend>
    {selectedRoutes.filter(route => route.id && disabledIds.has(route.id)).map(route =>
      <p key={route.label} className="field-help">{route.label}已停用，请重新选择已启用的模型。</p>)}
    {onVisionChange && onFallbackChange && <p className="field-help">聊天模型、图片理解模型和错误转发模型可以选择同一个模型，图片理解模型仍须启用图片理解能力。</p>}
    <label>{onVisionChange ? '聊天模型' : '默认模型'}<ModelSelect ariaLabel={onVisionChange ? '聊天模型' : '默认模型'} numeric required
      models={page.items} value={value} loading={busy} onOpen={() => void load()} onChange={next => onChange(Number(next))} /></label>
    {onVisionChange && <label>图片理解模型<ModelSelect ariaLabel="图片理解模型" numeric nullable vision
      models={page.items} value={visionValue ?? null} loading={busy} onOpen={() => void load()}
      placeholder="未配置：带图请求将报错" onChange={next => onVisionChange(next === null ? null : Number(next))} /></label>}
    {onVisionChange && <p className="field-help">带图请求必须配置图片理解模型，留空会报错。无法选择的模型尚未启用图片理解。请先在“供应商与模型”的模型列表中开启“图片理解”，开关会立即保存，重新打开模型选择框即可获取最新状态。</p>}
    {onFallbackChange && <label>错误转发模型<ModelSelect ariaLabel="错误转发模型" numeric nullable
      models={page.items} value={fallbackValue ?? null} loading={busy} onOpen={() => void load()}
      placeholder="不启用错误转发" onChange={next => onFallbackChange(next === null ? null : Number(next))} /></label>}
    {onFallbackChange && <p className="field-help">模型失败时由备用模型接管一次，继续使用已有工具结果。与本次使用的模型相同时不会重复请求同一模型。带图请求的备用模型也需启用图片理解。</p>}
    </fieldset>
  </>;
}
