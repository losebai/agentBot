import type { Page } from './ai-types';

export type MemoryScope = 'global' | 'session' | 'user' | 'group';
export type MemoryKind = 'relationship' | 'preference' | 'agreement' | 'task' | 'fact' | 'correction';
export type MemoryStatus = 'active' | 'pending' | 'disputed' | 'expired' | 'merged' | 'deleted';
export interface KnowledgeDraft { title: string; content: string; tags: string[] }
export interface KnowledgeEntry extends KnowledgeDraft { id: number; created_at: string }
export interface MemoryDraft {
  scope: MemoryScope; key: string; content: string; tags: string[];
  scope_id?: string; bot_id?: string; target?: string;
  topic?: string; kind?: MemoryKind; valid_from?: string | null; valid_until?: string | null;
}
/** 来源身份来自服务端；未知字段不能由页面猜测为本人确认。 */
export interface MemorySource {
  id: number; source_kind: string; source_ref: string | null; source_time: string | null;
  excerpt: string; source_actor_id?: string | null; recorded_at?: string | null;
}
export interface MemoryEntry {
  id: number; scope_id: string; scope: MemoryScope; key: string;
  scope_title?: string | null;
  content: string; tags: string[]; created_at: string;
  material_ids?: string[];
  revision: number; topic: string; kind: MemoryKind; status: MemoryStatus;
  effective_status: MemoryStatus | 'future'; source_kind: string; importance: number;
  observed_at: string | null; valid_from: string | null; valid_until: string | null;
  last_used_at: string | null; updated_at: string | null; sources: MemorySource[];
  subject_type: string; subject_id: string;
  created_time_basis?: string; updated_time_basis?: string;
  conflict_set_id?: string | null;
}
export interface MemoryUpdate {
  expected_revision: number; reason: string; content?: string; tags?: string[]; topic?: string;
  valid_from?: string | null; valid_until?: string | null;
}
export interface MemoryRevision {
  id: number; memory_id: number; revision: number; operation: string;
  snapshot: MemoryEntry; reason: string; created_at: string;
}
export interface MemoryVersion { id: number; expected_revision: number }
export interface MemoryMerge {
  target_id: number; items: MemoryVersion[]; reason: string; content?: string;
}
export interface MemoryResolve {target_id: number; items: MemoryVersion[]; reason: string}
export interface MemoryPreview {
  items: MemoryEntry[]; digest: string; action: 'merge' | 'expire' | 'forget'; target_id?: number;
}
export interface TagCount { tag: string; count: number }
export interface MemoryScopeInfo {
  scope_id: string; scope: MemoryScope; count: number; scope_title?: string | null;
}
export interface KnowledgeFilters { query?: string; tag?: string }
export interface MemoryFilters extends KnowledgeFilters {
  scope?: MemoryScope | ''; scope_id?: string; topic?: string;
  status?: MemoryStatus | 'future' | ''; kind?: MemoryKind | '';
}
export type TaggedEntry = KnowledgeEntry | MemoryEntry;
export type TaggedPage = Page<TaggedEntry>;
