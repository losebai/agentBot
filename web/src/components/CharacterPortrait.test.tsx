import { act, renderHook } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { useCharacterPortraits } from './CharacterPortrait';

afterEach(() => vi.unstubAllGlobals());

/** 模拟浏览器解码完成顺序，覆盖换图竞态及对象 URL 的释放。 */
it('快速换图仅保留最后选择，保存新角色后迁移预览并在卸载释放', async () => {
  const pending: (() => void)[] = [];
  vi.stubGlobal('Image', class {
    src = '';
    decode() { return new Promise<void>(resolve => pending.push(resolve)); }
  });
  const revoke = vi.fn();
  vi.stubGlobal('URL', { createObjectURL: vi.fn().mockReturnValueOnce('blob:first').mockReturnValueOnce('blob:last'), revokeObjectURL: revoke });
  const { result, unmount } = renderHook(() => useCharacterPortraits());
  const file = new File(['picture'], 'portrait.png', { type: 'image/png' });
  let first!: Promise<void>, last!: Promise<void>;
  act(() => { first = result.current.choose('new', file); last = result.current.choose('new', file); });
  await act(async () => { pending[1](); await last; });
  await act(async () => { pending[0](); await first; });
  expect(result.current.images).toEqual({ new: 'blob:last' });
  expect(revoke).toHaveBeenCalledWith('blob:first');
  act(() => result.current.move('new', '7'));
  expect(result.current.images).toEqual({ '7': 'blob:last' });
  unmount();
  expect(revoke).toHaveBeenCalledWith('blob:last');
});

/** 错误按身份隔离；删除角色会使正在解码的图片失效，不能再次写回。 */
it('坏图提示只属于当前角色，删除会取消迟到的图片结果', async () => {
  let resolve!: () => void;
  vi.stubGlobal('Image', class {
    src = '';
    decode() { return new Promise<void>(done => { resolve = done; }); }
  });
  const revoke = vi.fn();
  vi.stubGlobal('URL', { createObjectURL: vi.fn(() => 'blob:pending'), revokeObjectURL: revoke });
  const { result } = renderHook(() => useCharacterPortraits());
  await act(async () => { await result.current.choose('1', new File(['bad'], 'test.svg', { type: 'image/svg+xml' })); });
  expect(result.current.errors['1']).toContain('PNG');
  expect(result.current.errors['2']).toBeUndefined();
  let loading!: Promise<void>;
  act(() => { loading = result.current.choose('2', new File(['picture'], 'portrait.png', { type: 'image/png' })); });
  act(() => result.current.remove('2'));
  await act(async () => { resolve(); await loading; });
  expect(result.current.images).toEqual({});
  expect(revoke).toHaveBeenCalledWith('blob:pending');
});
