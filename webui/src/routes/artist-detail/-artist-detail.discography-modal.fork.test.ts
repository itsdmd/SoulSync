import { describe, expect, it } from 'vitest';

import {
  DISCOG_DEFAULT_FILTERS,
  discogCardView,
  discogCardVisible,
  releaseOwnershipStatus,
  releasesFromPageDiscography,
} from './-artist-detail.discography-modal';

/** fork (itsdmd/SoulSync): the "Hide owned" filter of Download Discography. */
describe('discography modal: hide owned', () => {
  const page = {
    albums: [
      {
        id: 'full',
        title: 'Owned In Full',
        owned: true,
        track_completion: { owned_tracks: 10, total_tracks: 10, percentage: 100 },
      },
      {
        id: 'part',
        title: 'Half There',
        owned: true,
        track_completion: { owned_tracks: 4, total_tracks: 10, percentage: 40 },
      },
      { id: 'none', title: 'Missing', owned: false, track_completion: 0 },
      { id: 'checking', title: 'Still Checking', owned: null },
      { id: 'legacy', title: 'Owned, No Counts', owned: true },
    ],
  };
  const releases = releasesFromPageDiscography(page as never);
  const byId = (id: string) => releases.find((r) => r.id === id)!;

  it('reads ownership from what the artist page resolved', () => {
    expect(releaseOwnershipStatus(byId('full'))).toBe('completed');
    expect(releaseOwnershipStatus(byId('part'))).toBe('partial');
    expect(releaseOwnershipStatus(byId('none'))).toBe('unknown');
    expect(releaseOwnershipStatus(byId('checking'))).toBe('unknown');
    expect(releaseOwnershipStatus(byId('legacy'))).toBe('completed');
  });

  it('marks owned cards and leaves them unchecked by default', () => {
    const full = discogCardView(byId('full'), {});
    expect(full.statusClass).toBe('owned');
    expect(full.checkedByDefault).toBe(false);
    const part = discogCardView(byId('part'), {});
    expect(part.statusClass).toBe('partial');
    expect(part.checkedByDefault).toBe(true);
    expect(discogCardView(byId('none'), {}).checkedByDefault).toBe(true);
  });

  it('hides only fully owned releases when the filter is on', () => {
    const visible = (hideOwned: boolean) =>
      releases
        .filter((r) =>
          discogCardVisible(discogCardView(r, {}), r._type, {
            ...DISCOG_DEFAULT_FILTERS,
            hideOwned,
          }),
        )
        .map((r) => r.id);
    expect(visible(false)).toEqual(['full', 'part', 'none', 'checking', 'legacy']);
    expect(visible(true)).toEqual(['part', 'none', 'checking']);
  });

  it('an explicit completion-cache entry still wins over the release', () => {
    const view = discogCardView(byId('full'), { albums: [{ id: 'full', status: 'partial' }] });
    expect(view.statusClass).toBe('partial');
  });
});
