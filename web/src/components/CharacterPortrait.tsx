import { PictureOutlined, UploadOutlined, UserOutlined } from '@ant-design/icons';
import { useEffect, useRef, useState } from 'react';

/** 人物图只用于本页预览；对象 URL 不进入角色接口或提示词，退出页面释放资源。 */
export function useCharacterPortraits() {
  const [images, setImages] = useState<Record<string, string>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const urls = useRef<Record<string, string>>({});
  const requests = useRef<Record<string, number>>({});
  const mounted = useRef(false);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      Object.values(urls.current).forEach(url => URL.revokeObjectURL(url));
      urls.current = {};
    };
  }, []);

  /** 解码成功后才替换图像；失败保留原图，快速重复选择只接受最后一次。 */
  async function choose(key: string, file: File) {
    const version = (requests.current[key] ?? 0) + 1;
    requests.current[key] = version;
    if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type) || file.size > 5 * 1024 * 1024) {
      setErrors(previous => ({ ...previous, [key]: '请选择不超过 5 MB 的 PNG、JPG 或 WebP 图片。' }));
      return;
    }
    setErrors(previous => ({ ...previous, [key]: '' }));
    const url = URL.createObjectURL(file);
    try {
      const image = new Image();
      image.src = url;
      await image.decode();
      if (!mounted.current || requests.current[key] !== version) {
        URL.revokeObjectURL(url);
        return;
      }
      if (urls.current[key]) URL.revokeObjectURL(urls.current[key]);
      urls.current = { ...urls.current, [key]: url };
      setImages({ ...urls.current });
    } catch {
      URL.revokeObjectURL(url);
      if (mounted.current && requests.current[key] === version) setErrors(previous => ({ ...previous, [key]: '图片无法解码，请选择有效的人物图片。' }));
    }
  }

  /** 新建保存后迁移预览身份，避免形象因服务器分配 ID 而丢失。 */
  function move(from: string, to: string) {
    if (from === to) return;
    requests.current[from] = (requests.current[from] ?? 0) + 1;
    if (!urls.current[from]) return;
    if (urls.current[to]) URL.revokeObjectURL(urls.current[to]);
    urls.current[to] = urls.current[from];
    delete urls.current[from];
    setImages({ ...urls.current });
  }

  /** 删除角色时同时移除预览并取消尚未完成的本地解码。 */
  function remove(key: string) {
    requests.current[key] = (requests.current[key] ?? 0) + 1;
    if (urls.current[key]) URL.revokeObjectURL(urls.current[key]);
    delete urls.current[key];
    setImages({ ...urls.current });
  }

  return { images, errors, choose, move, remove };
}

/** 以静态人物插画预览角色形象；明确区分本地预览与服务器保存的角色配置。 */
export function CharacterPortrait({ image, name, disabled, onChange, error }: {
  image?: string; name: string; disabled: boolean; onChange: (file: File) => void; error: string;
}) {
  const input = useRef<HTMLInputElement>(null);
  return <div className="character-portrait-panel">
    <div className={`character-portrait${image ? ' has-image' : ''}`}>
      <span className="character-portrait-label"><PictureOutlined />角色形象</span>
      {image ? <img src={image} alt={`${name}的人物形象`} /> : <div className="character-portrait-empty">
        <div className="character-avatar"><UserOutlined /></div><strong>给角色一个形象</strong><span>添加你喜欢的 3D 风格人物插画</span>
      </div>}
      <button type="button" disabled={disabled} className="character-image-picker" onClick={() => input.current?.click()}>
        <UploadOutlined />{image ? '更换预览图片' : '选择人物图片'}</button>
      <input ref={input} type="file" aria-label="选择人物形象" accept="image/png,image/jpeg,image/webp" hidden disabled={disabled}
        onChange={event => { const file = event.target.files?.[0]; if (file) onChange(file); event.target.value = ''; }} />
    </div>
    <p className="character-image-note">仅本页预览 · PNG / JPG / WebP · 最大 5 MB</p>
    {error && <p className="character-image-error" role="alert">{error}</p>}
  </div>;
}
