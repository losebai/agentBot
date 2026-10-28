/** 同一原件可以既由生成器产出又经 LLM 发送，分类分别保留。 */
export type MaterialCategory = 'received' | 'sent' | 'generated' | 'uploaded';

/** 管理员素材元数据，不包含磁盘位置、临时地址或会话认证凭据。 */
export interface LibraryMaterial {
  id: string;
  name: string;
  kind: 'image' | 'file';
  mime_type: string;
  size_bytes: number;
  sha256: string;
  description: string;
  created_at: string;
  entry: 'admin' | 'qq';
  session_id: string;
  categories: MaterialCategory[];
  tags: string[];
}

/** 会话筛选使用完整 ID，与来源条件共同匹配已有素材。 */
export interface MaterialLibraryFilters {
  query: string;
  kind: '' | LibraryMaterial['kind'];
  entry: '' | LibraryMaterial['entry'];
  session_id: string;
  category: '' | MaterialCategory;
  tag: string;
}
