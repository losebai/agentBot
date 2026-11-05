import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { AutonomyApi } from '../autonomy-api';
import { AutonomyEvolution } from './AutonomyEvolution';

it('先预览本人研究，再按同一证据版本手动应用，失败保留研究', async () => {
  const api = new AutonomyApi('offline');
  const research = { profile_id: 1, revision: 3, preview_key: 'a'.repeat(64),
    before: { verbosity: 0, warmth: 0, humor: 0, initiative: 0 },
    after: { verbosity: -1, warmth: 0, humor: 0, initiative: 0 },
    changes: [{ dimension: 'verbosity' as const, before: 0, after: -1, net_support: -3, dates: 3, eligible: true, evidence_ids: [1, 2, 3] }] };
  const preview = vi.spyOn(api, 'evolution').mockResolvedValue({ research });
  const advance = vi.spyOn(api, 'advanceEvolution').mockRejectedValueOnce(new Error('证据已变化'));
  render(<AutonomyEvolution api={api} id="alice" />);
  expect(screen.queryByRole('button', { name: '手动演进' })).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '预览演进研究' }));
  expect(await screen.findByText('0 → -1')).toBeVisible();
  expect(preview).toHaveBeenCalledWith('alice', expect.any(AbortSignal));
  fireEvent.click(screen.getByRole('button', { name: '手动演进' }));
  await waitFor(() => expect(advance).toHaveBeenCalledWith('alice', research));
  expect(await screen.findByText('证据已变化')).toBeVisible();
  expect(screen.getByText('0 → -1')).toBeVisible();
});
