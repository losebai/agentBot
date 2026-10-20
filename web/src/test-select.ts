import { fireEvent, waitFor, within } from '@testing-library/react';
import { expect } from 'vitest';
import userEvent from '@testing-library/user-event';

/** 使用实际弹层选择选项，覆盖组件库的鼠标交互与文本可访问名称。 */
export async function chooseSelect(input: HTMLElement, label: string | RegExp) {
  await userEvent.click(input);
  const option = await waitFor(() => {
    const element = document.getElementById(input.getAttribute('aria-controls') ?? '');
    if (!element) throw new Error('选择框的列表尚未打开');
    const option = within(element).getByText(label).closest('[role="option"]');
    if (!option) throw new Error('找到的文本不是列表选项');
    expect(option).toBeVisible();
    return option;
  });
  await userEvent.click(option);
}

/** 从实际清空按钮触发空值契约，不给搜索输入伪造选中值。 */
export function clearSelect(input: HTMLElement) {
  const clear = input.closest('.ant-select')?.querySelector('.ant-select-clear');
  if (!clear) throw new Error('下拉没有可用的清空按钮');
  fireEvent.mouseDown(clear);
}

/** 选中名称由选择框展示，搜索输入的 value 仅表示当前搜索词。 */
export function selectDisplay(input: HTMLElement) {
  return input.closest('.ant-select')!;
}
