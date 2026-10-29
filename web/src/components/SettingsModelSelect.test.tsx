import { fireEvent, render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { SettingsModelSelect } from './SettingsModelSelect';

const models = [
  { id: 1, revision: 1, provider_id: 10, provider_name: '供应商甲', upstream_model: 'shared', reference: 'model:1', alias: 'legacy', enabled: true },
  { id: 2, revision: 1, provider_id: 20, provider_name: '供应商乙', upstream_model: 'shared', reference: 'model:2', alias: null, enabled: true },
];

it('旧别名显示供应商与上游模型，搜索供应商后提交稳定引用', async () => {
  const changed = vi.fn();
  render(<><label htmlFor="model">模型</label><SettingsModelSelect id="model" value="legacy" readOnly={false} nullable
    directory={{ page: { items: models, next_cursor: null }, busy: false, error: '', load: vi.fn() }} onChange={changed} /></>);
  expect(screen.getByText('供应商甲 / shared')).toBeVisible();
  const input = screen.getByRole('combobox', { name: '模型' });
  fireEvent.mouseDown(input);
  fireEvent.change(input, { target: { value: '供应商乙' } });
  fireEvent.click(await screen.findByText('供应商乙 / shared'));
  expect(changed).toHaveBeenCalledWith('model:2');
});

it('加载失败保留未知引用，目录刷新与只读不会更换模型', () => {
  const changed = vi.fn();
  const props = { id: 'model', value: 'model:404', readOnly: true, nullable: true, onChange: changed,
    directory: { page: { items: models, next_cursor: null }, busy: false, error: '读取失败', load: vi.fn() } };
  const view = render(<SettingsModelSelect {...props} />);
  expect(screen.getByText(/保留当前配置/)).toBeVisible();
  view.rerender(<SettingsModelSelect {...props} directory={{ ...props.directory, busy: true }} />);
  expect(changed).not.toHaveBeenCalled();
  expect(screen.getByRole('combobox')).toBeDisabled();
});

it('旧别名与另一模型的稳定引用重名时保留未确认状态，不猜测绑定', () => {
  const changed = vi.fn();
  render(<SettingsModelSelect id="model" value="model:2" readOnly={false} nullable
    directory={{ page: { items: [{ ...models[0], alias: 'model:2' }, models[1]], next_cursor: null }, busy: false, error: '', load: vi.fn() }} onChange={changed} />);
  expect(screen.getByText('保留当前配置（model:2）')).toBeVisible();
  expect(changed).not.toHaveBeenCalled();
});

it.each([null, 'next-page'])('规范格式旧别名指向其他ID时，即使目标不在目录也不能确认绑定（分页：%s）', async next_cursor => {
  const changed = vi.fn();
  render(<SettingsModelSelect id="model" value="model:2" readOnly={false} nullable
    directory={{ page: { items: [{ ...models[0], alias: 'model:2' }], next_cursor }, busy: false, error: '', load: vi.fn() }} onChange={changed} />);
  expect(screen.getByText('保留当前配置（model:2）')).toBeVisible();
  expect(screen.getByText('当前配置存在模型身份歧义，请先完成模型身份升级。')).toBeVisible();
  expect(changed).not.toHaveBeenCalled();
  fireEvent.mouseDown(screen.getByRole('combobox'));
  fireEvent.click(await screen.findByText('供应商甲 / shared'));
  expect(changed).toHaveBeenCalledWith('model:1');
});

it('表情包独立模型仅允许视觉模型，保留失效选择并允许清空', async () => {
  const changed = vi.fn();
  render(<SettingsModelSelect id="sticker-model" value="model:1" readOnly={false} nullable vision
    directory={{ page: { items: [models[0], { ...models[1], media: { vision_enabled: true,
      image_api_profile: 'none', audio_chat_enabled: false } }], next_cursor: null }, busy: false, error: '', load: vi.fn() }}
    onChange={changed} />);
  expect(screen.getByText('当前配置的模型未开启视觉能力，请选择支持识图的模型。')).toBeVisible();
  expect(changed).not.toHaveBeenCalled();
  fireEvent.mouseDown(screen.getByRole('combobox'));
  const textOnly = (await screen.findAllByText('供应商甲 / shared')).find(item => item.closest('.ant-select-item'))!;
  expect(textOnly.closest('.ant-select-item')).toHaveClass('ant-select-item-option-disabled');
  fireEvent.click(await screen.findByText('供应商乙 / shared'));
  expect(changed).toHaveBeenCalledWith('model:2');
});
