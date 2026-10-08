import { describe, expect, it } from 'vitest';

import type { Discography } from './-artist-detail.types';

import { albumCheckedEvent, folderMatchRequest } from './-artist-detail.fork';

describe('fork artist page glue', () => {
  it('lists every release once, in page order, without gap-fill cards', () => {
    const discography = {
      source: 'deezer',
      albums: [
        { id: 1, title: 'A' },
        { id: 2, title: 'From elsewhere', _gap_source: 'spotify' },
      ],
      eps: [{ id: 3, title: 'E' }],
      singles: [{ id: 1, title: 'A again' }, { title: 'No id' }, { id: 4, title: 'S' }],
    } as unknown as Discography;
    expect(folderMatchRequest('Artist', discography)).toEqual({
      artist: { name: 'Artist' },
      source: 'deezer',
      releases: [
        { id: '1', name: 'A', type: 'Album' },
        { id: '3', name: 'E', type: 'EP' },
        { id: '4', name: 'S', type: 'Single' },
      ],
    });
    expect(folderMatchRequest(undefined, {} as Discography).releases).toEqual([]);
  });

  it('reads a checked album as a completion event', () => {
    const detail = { id: 'al1', status: 'partial', owned_tracks: 2, expected_tracks: 3 };
    expect(albumCheckedEvent(new CustomEvent('x', { detail }))).toEqual({
      ...detail,
      type: 'completion',
    });
    expect(albumCheckedEvent(new CustomEvent('x'))).toBeNull();
    expect(albumCheckedEvent(new CustomEvent('x', { detail: { status: 'missing' } }))).toBeNull();
  });
});
