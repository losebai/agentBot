import { PictureOutlined, UploadOutlined, UserOutlined } from '@ant-design/icons';
import { useEffect, useRef, useState } from 'react';
import type { AgentApi } from '../agent-api';
import type { Agent } from '../agent-types';
import { errorMessage } from '../api';

/** 认证读取持久图；本地修改保留原文件，点击保存时与角色配置共同提交。 */
export function useCharacterPortraits(api?: AgentApi, items: Agent[] = []) {
  const [images, setImages] = useState<Record<string, string>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const urls = useRef<Record<string, string>>({});
  const requests = useRef<Record<string, number>>({});
  const mounted = useRef(false);
  const files = useRef<Record<string, File>>({});
  const hashes = useRef<Record<string, string>>({});
  const [reload, setReload] = useState(0);
  const decoding = useRef<Record<string, boolean>>({});
  const [preparing, setPreparing] = useState<Record<string, boolean>>({});

  useEffect(() => {
    if (!api) return;
    const controller = new AbortController();
    // 逐个读取当前页，取消后释放对象 URL；本地待保存图片优先于服务器快照。
    async function read() {
      for (const agent of items) {
        const key = String(agent.id), hash = agent.portrait_sha256;
        if (files.current[key] || decoding.current[key]) continue;
        if (!hash) {
          if (urls.current[key]) URL.revokeObjectURL(urls.current[key]);
          delete urls.current[key]; delete hashes.current[key];
          setImages({ ...urls.current });
          continue;
        }
        if (hashes.current[key] === hash) continue;
        const version = requests.current[key] ?? 0;
        try {
          const blob = await api!.portrait(agent.id, controller.signal);
          if (controller.signal.aborted) return;
          if (files.current[key] || decoding.current[key] || (requests.current[key] ?? 0) !== version) continue;
          const url = URL.createObjectURL(blob);
          if (urls.current[key]) URL.revokeObjectURL(urls.current[key]);
          urls.current[key] = url; hashes.current[key] = hash;
          setImages({ ...urls.current });
          setErrors(previous => ({ ...previous, [key]: '' }));
        } catch (failure) {
          if (controller.signal.aborted) return;
          if ((requests.current[key] ?? 0) === version) setErrors(previous => ({ ...previous, [key]: errorMessage(failure) }));
        }
      }
    }
    void read();
    return () => controller.abort();
  }, [api, items, reload]);

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
    decoding.current[key] = false;
    setPreparing(previous => ({ ...previous, [key]: false }));
    if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type) || file.size > 5 * 1024 * 1024) {
      setErrors(previous => ({ ...previous, [key]: '请选择不超过 5 MB 的 PNG、JPG 或 WebP 图片。' }));
      return;
    }
    setErrors(previous => ({ ...previous, [key]: '' }));
    decoding.current[key] = true;
    setPreparing(previous => ({ ...previous, [key]: true }));
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
      files.current[key] = file;
      urls.current = { ...urls.current, [key]: url };
      setImages({ ...urls.current });
    } catch {
      URL.revokeObjectURL(url);
      if (mounted.current && requests.current[key] === version) setErrors(previous => ({ ...previous, [key]: '图片无法解码，请选择有效的人物图片。' }));
    } finally {
      if (mounted.current && requests.current[key] === version) {
        decoding.current[key] = false;
        setPreparing(previous => ({ ...previous, [key]: false }));
        setReload(value => value + 1);
      }
    }
  }

  /** 新建保存后迁移预览身份，避免形象因服务器分配 ID 而丢失。 */
  function move(from: string, to: string) {
    delete files.current[from];
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
    delete urls.current[key]; delete files.current[key]; delete hashes.current[key];
    delete decoding.current[key];
    setPreparing(previous => ({ ...previous, [key]: false }));
    setErrors(previous => ({ ...previous, [key]: '' }));
    setImages({ ...urls.current });
  }

  /** 撤销待保存图片后重新读取服务器版本，不依赖列表重新加载。 */
  function reset(key: string) {
    remove(key);
    setReload(value => value + 1);
  }

  /** Base64 仅出现在明确保存请求中；读取失败不丢弃本地预览或原文件。 */
  async function upload(key: string) {
    const file = files.current[key];
    if (!file) return undefined;
    const data = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).split(',')[1]);
      reader.onerror = () => reject(new Error('人物图读取失败，请重新选择图片。'));
      reader.readAsDataURL(file);
    });
    return { mime_type: file.type, data };
  }

  return { images, errors, preparing, choose, move, remove, reset, upload };
}

/** 以静态人物插画展示角色；新选图片在保存角色后持久化。 */
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
        <UploadOutlined />{image ? '更换人物图片' : '选择人物图片'}</button>
      <input ref={input} type="file" aria-label="选择人物形象" accept="image/png,image/jpeg,image/webp" hidden disabled={disabled}
        onChange={event => { const file = event.target.files?.[0]; if (file) onChange(file); event.target.value = ''; }} />
    </div>
    <p className="character-image-note">点击“保存 Agent”持久保存 · PNG / JPG / WebP · 最大 5 MB</p>
    {error && <p className="character-image-error" role="alert">{error}</p>}
  </div>;
}
