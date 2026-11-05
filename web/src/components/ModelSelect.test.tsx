import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useState } from 'react';
import { expect, it, vi } from 'vitest';
import { ModelSelect } from './ModelSelect';
import { clearSelect } from '../test-select';

const models = [
  { id: 1, revision: 1, provider_id: 1, provider_name: '供应商', upstream_model: 'text', reference: 'model:1', enabled: true },
  { id: 2, revision: 1, provider_id: 1, provider_name: '供应商', upstream_model: 'vision', reference: 'model:2', enabled: true,
    media: { vision_enabled: true, image_api_profile: 'none' as const, audio_chat_enabled: false } },
];

it('键盘选择保留 Agent 整数类型，清空提交 null，必填状态阻止空表单', async () => {
  const changed = vi.fn();
  /** 使用真实受控表单检验选择后的浏览器约束与类型。 */
  function Form() {
    const [value, setValue] = useState<number | null>(null);
    return <form aria-label="路由表单"><ModelSelect ariaLabel="模型" models={models} value={value} numeric nullable required
      onChange={next => { changed(next); setValue(next === null ? null : Number(next)); }} /></form>;
  }
  render(<Form />);
  const form = screen.getByRole('form') as HTMLFormElement;
  expect(form.checkValidity()).toBe(false);
  const input = screen.getByRole('combobox', { name: '模型' });
  input.focus();
  fireEvent.keyDown(input, { key: 'ArrowDown', code: 'ArrowDown', keyCode: 40 });
  await screen.findByRole('option', { name: 'text' });
  fireEvent.keyDown(input, { key: 'Enter', code: 'Enter', keyCode: 13 });
  expect(changed).toHaveBeenLastCalledWith(1);
  expect(form.checkValidity()).toBe(true);
  clearSelect(input);
  expect(changed).toHaveBeenLastCalledWith(null);
  expect(form.checkValidity()).toBe(false);
});

it('图片路由明确禁用没有识图能力的模型，不触发错误选择', async () => {
  const changed = vi.fn();
  render(<ModelSelect ariaLabel="图片模型" models={models} value={null} numeric vision onChange={changed} />);
  fireEvent.mouseDown(screen.getByRole('combobox', { name: '图片模型' }));
  const text = await screen.findByRole('option', { name: 'text' });
  fireEvent.click(text);
  expect(changed).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('option', { name: 'vision' }));
  expect(changed).toHaveBeenLastCalledWith(2);
});

it('供应商只用于分组，上游自带前缀原样显示且不改变稳定身份', async () => {
  const changed = vi.fn();
  const item = { ...models[0], provider_name: 'zcode', upstream_model: 'zcode/GLM-5.3' };
  render(<ModelSelect ariaLabel="模型" models={[item]} value="model:1" onChange={changed} />);
  expect(screen.getByText('zcode/GLM-5.3')).toBeVisible();
  fireEvent.mouseDown(screen.getByRole('combobox', { name: '模型' }));
  await waitFor(() => expect(screen.getByText('zcode', { selector: '.ant-select-item-group' })).toBeVisible());
  const option = screen.getByRole('option', { name: 'zcode/GLM-5.3' });
  expect(option).toHaveTextContent(/^zcode\/GLM-5.3$/);
  expect(screen.queryByText('zcode / zcode/GLM-5.3')).not.toBeInTheDocument();
  fireEvent.click(option);
  expect(changed).not.toHaveBeenCalled();
});
