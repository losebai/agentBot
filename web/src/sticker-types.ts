/** 表情包元数据不包含磁盘路径，原件与预览统一经管理员认证读取。 */
export interface StickerView {
  id: string;
  name: string;
  tags: string[];
  description: string;
  emotion: string;
  usage_hint: string;
  enabled: boolean;
  revision: number;
  mime_type: string;
  size_bytes: number;
  sha256: string;
  width: number;
  height: number;
  frame_count: number;
  animated: boolean;
  analysis_status: 'pending' | 'ready' | 'failed';
  analysis_error: string | null;
  created_at: string;
  updated_at: string;
}

/** 只提交管理员实际编辑的字段，避免覆盖并发识图产生的新说明。 */
export type StickerPatch = Partial<Pick<StickerView,
  'name' | 'tags' | 'description' | 'emotion' | 'usage_hint' | 'enabled'>> & { expected_revision: number };
export interface StickerPage { items: StickerView[]; total: number; next_offset: number | null }
export interface StickerMatch { sticker: StickerView; score: number; reason: string }
