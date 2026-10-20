import { Select } from 'antd';
import { Children, isValidElement, useEffect, useId, useRef, type ReactNode } from 'react';
import type { SelectProps } from 'antd';

/** 保留 HTML 表单约束：选项搜索输入不代表选中值，必填状态由真实选择计算。 */
export function useSelectionValidity(value: string | number | null | undefined, required: boolean, disabled?: boolean) {
  const root = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    const input = root.current?.querySelector<HTMLInputElement>('input[role="combobox"]');
    input?.setCustomValidity(required && !disabled && !value ? '请选择一个选项' : '');
  }, [value, required, disabled]);
  return root;
}

/** 普通枚举统一使用 Ant Design；回调直接传值，保留旧原生选择的字符串契约。 */
export function ChoiceSelect({ children, value, onChange, required = false, ...props }: Omit<SelectProps<string>, 'onChange' | 'value' | 'options' | 'children'> & {
  children: ReactNode; value: string | number | undefined; onChange: (value: string) => void; required?: boolean;
}) {
  const root = useSelectionValidity(value, required, props.disabled);
  const generatedId = useId();
  const options = Children.toArray(children).filter(isValidElement).map(child => {
    const option = child.props as { value?: string | number; children: ReactNode; disabled?: boolean; hidden?: boolean };
    return { value: String(option.value ?? option.children), label: option.children, disabled: option.disabled, hidden: option.hidden };
  }).filter(option => !option.hidden || option.value === String(value ?? ''));
  return <span ref={root} className="choice-control"><Select showSearch optionFilterProp="label" {...props} id={props.id ?? generatedId} value={String(value ?? '')} options={options}
    aria-required={required || undefined} virtual={false} onChange={onChange} /></span>;
}
