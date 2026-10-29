import { useModelDirectory } from './useModelDirectory';
import { ambiguousModelReference, ModelSelect, selectedModel } from './ModelSelect';

export const modelSettingNames = new Set([
  'chat_model', 'chat_vision_model', 'chat_image_model', 'chat_tts_model',
  'chat_memory_embedding_model',
  'chat_auto_speak_decision_model',
  'chat_sticker_model',
]);

/** 设置字段复用同一模型目录，分类不可见时取消读取。 */
export const useSettingsModels = useModelDirectory;

/** 旧别名可读，新选择写稳定引用；视觉限定只影响候选，不更换已有配置与草稿。 */
export function SettingsModelSelect({ id, value, readOnly, nullable, directory, onChange, vision = false }: {
  id: string; value: string; readOnly: boolean; nullable: boolean;
  directory: ReturnType<typeof useSettingsModels>; onChange: (value: string) => void;
  vision?: boolean;
}) {
  const { page, busy, error } = directory;
  const current = selectedModel(page.items, value);
  const missing = !!value && (!current || !current.enabled || (vision && !current.media?.vision_enabled));
  const ambiguous = !!value && ambiguousModelReference(page.items, value);
  return <>
    <ModelSelect id={id} models={page.items} value={value} disabled={readOnly} loading={busy}
      onOpen={() => void directory.load()} nullable={nullable} vision={vision} placeholder={nullable ? '未配置' : '选择模型'}
      describedBy={missing ? `${id}-help` : undefined} onChange={next => onChange(String(next ?? ''))} />
    {missing && !busy && <small id={`${id}-help`}>{ambiguous ? '当前配置存在模型身份歧义，请先完成模型身份升级。' : error
      ? '当前配置尚未确认，请重新打开模型选择框。'
      : current?.enabled && vision && !current.media?.vision_enabled ? '当前配置的模型未开启视觉能力，请选择支持识图的模型。'
      : page.next_cursor ? '当前配置不在已加载的启用模型中，请加载更多或重新选择。'
        : '当前配置的模型未启用或不存在，请重新选择。'}</small>}
  </>;
}
