import { Select } from 'antd';
import { useId } from 'react';
import type { AiModel } from '../ai-types';
import { useSelectionValidity } from './ChoiceSelect';

/** 旧别名不能占用其他记录的稳定引用命名空间；分页未加载目标时同样拒绝确认。 */
export function ambiguousModelReference(models: AiModel[], value: string) {
  if (/^model:[1-9][0-9]*$/.test(value)
    && models.some(model => model.alias === value && model.reference !== value)) return true;
  return models.filter(model => model.reference === value || model.alias === value).length > 1;
}

/** 模型显示名不参与绑定；保留前缀的非法格式与歧义不得被旧别名映射掩盖。 */
export function selectedModel(models: AiModel[], value: string) {
  if ((value.startsWith('model:') && !/^model:[1-9][0-9]*$/.test(value))
    || ambiguousModelReference(models, value)) return undefined;
  const candidates = models.filter(model => model.reference === value || model.alias === value);
  return candidates.length === 1 ? candidates[0] : undefined;
}

/** 供应商分组和搜索共用完整名称，同名上游模型仍以记录 ID 区分。 */
export function ModelSelect({ id, models, value, onChange, disabled, loading, placeholder = '选择模型',
  nullable = false, numeric = false, vision = false, required = false, describedBy, ariaLabel }: {
  id?: string; models: AiModel[]; value: string | number | null; onChange: (value: string | number | null) => void;
  disabled?: boolean; loading?: boolean; placeholder?: string; nullable?: boolean; numeric?: boolean;
  vision?: boolean; required?: boolean; describedBy?: string; ariaLabel?: string;
}) {
  const selected = numeric ? models.find(model => model.id === value) : selectedModel(models, String(value ?? ''));
  const generatedId = useId();
  const current = selected ? numeric ? selected.id : selected.reference : value || undefined;
  const root = useSelectionValidity(current, required, disabled);
  const groups = new Map<number, { label: string; options: { value: string | number; label: string; disabled: boolean; search: string }[] }>();
  for (const model of models) {
    if (!model.enabled) continue;
    if (!groups.has(model.provider_id)) groups.set(model.provider_id, { label: model.provider_name, options: [] });
    groups.get(model.provider_id)!.options.push({ value: numeric ? model.id : model.reference,
      label: `${model.provider_name} / ${model.upstream_model}`,
      disabled: vision && !model.media?.vision_enabled, search: `${model.provider_name} ${model.upstream_model}` });
  }
  const options = [...groups.values()];
  if (current && !selected) {
    // 旧别名可能与另一记录的规范引用重名；冲突项不能冒充已确认的当前模型。
    for (const group of options) group.options = group.options.filter(option => option.value !== current);
  }
  // 未加载或停用的已有身份只作为禁用项保留，不产生 onChange，也不改选其他记录。
  if (current && (!selected || !selected.enabled)) options.unshift({ label: '当前配置', options: [{
    value: current, label: selected ? `${selected.provider_name} / ${selected.upstream_model}（已停用）` : `保留当前配置（${current}）`,
    disabled: true, search: '',
  }] });
  return <span ref={root} className="choice-control"><Select id={id ?? generatedId} className="model-select" value={current} disabled={disabled} loading={loading}
    showSearch allowClear={nullable} placeholder={placeholder} aria-required={required || undefined}
    aria-label={ariaLabel} aria-describedby={describedBy} options={options} virtual={false}
    filterOption={(search, option) => !!option && 'search' in option && typeof option.search === 'string' && option.search.toLowerCase().includes(search.toLowerCase())}
    onChange={next => onChange(next ?? (numeric ? null : ''))} /></span>;
}
