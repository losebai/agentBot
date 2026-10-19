import type { Page } from './ai-types';

export type MemoryScope = 'global' | 'session' | 'user' | 'group';
export interface KnowledgeDraft { title: string; content: string; tags: string[] }
export interface KnowledgeEntry extends KnowledgeDraft { id: number; created_at: string }
export interface MemoryDraft {
  scope: MemoryScope; key: string; content: string; tags: string[];
  scope_id?: string; bot_id?: string; target?: string;
}
export interface MemoryEntry {
  id: number; scope_id: string; scope: MemoryScope; key: string;
  scope_title?: string | null;
  content: string; tags: string[]; created_at: string;
  material_ids?: string[];
}
export interface TagCount { tag: string; count: number }
export interface MemoryScopeInfo {
  scope_id: string; scope: MemoryScope; count: number; scope_title?: string | null;
}
export interface KnowledgeFilters { query?: string; tag?: string }
export interface MemoryFilters extends KnowledgeFilters { scope?: MemoryScope | ''; scope_id?: string }
export type TaggedEntry = KnowledgeEntry | MemoryEntry;
export type TaggedPage = Page<TaggedEntry>;
