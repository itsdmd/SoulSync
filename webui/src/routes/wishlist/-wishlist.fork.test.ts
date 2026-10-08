import { describe, expect, it } from 'vitest';

import type { ParsedWishlistTrack, WishlistArtistGroup } from './-wishlist.types';

import {
  forkArtistOf,
  forkFilterAlbumGroups,
  forkGroupByAlbum,
  forkGroupImages,
  forkIsAlbumGroup,
} from './-wishlist.fork';

const track = (
  id: string,
  artist: string,
  album: string,
  type: 'album' | 'single',
  failing = false,
): ParsedWishlistTrack => ({
  track: `Song ${id}`,
  artist,
  album,
  image: `${album}.jpg`,
  type,
  id,
  retry: failing ? 3 : 0,
  failing,
  lastTried: '',
  failReason: '',
});

const groups: WishlistArtistGroup[] = [
  {
    name: 'Polyphia',
    albums: [
      {
        name: 'Be Not Afraid',
        image: 'bna.jpg',
        tracks: [
          track('1', 'Polyphia', 'Be Not Afraid', 'album'),
          track('2', 'Polyphia', 'Be Not Afraid', 'album', true),
        ],
      },
      {
        name: 'Greatest Hits',
        image: '',
        tracks: [track('3', 'Polyphia', 'Greatest Hits', 'album')],
      },
    ],
    singles: [
      track('4', 'Polyphia', 'With Eyes to See', 'single'),
      track('5', 'Polyphia', 'Be Not Afraid', 'single'),
    ],
    total: 5,
    failingCount: 1,
  },
  {
    name: 'Other',
    albums: [
      {
        name: 'Greatest Hits',
        image: 'gh.jpg',
        tracks: [track('6', 'Other', 'Greatest Hits', 'album')],
      },
    ],
    singles: [],
    total: 1,
    failingCount: 0,
  },
];

describe('wishlist grouped by album (fork)', () => {
  const albums = forkGroupByAlbum(groups);

  it('makes one group per release, busiest first, keeping the artist', () => {
    expect(albums.map((g) => [g.name, g.forkArtist, g.total, g.failingCount])).toEqual([
      ['Be Not Afraid', 'Polyphia', 3, 1],
      ['Greatest Hits — Polyphia', 'Polyphia', 1, 0],
      ['With Eyes to See', 'Polyphia', 1, 0],
      ['Greatest Hits — Other', 'Other', 1, 0],
    ]);
    // an album and a single of the same release share a group
    expect(albums[0].albums[0].tracks.map((t) => t.id)).toEqual(['1', '2']);
    expect(albums[0].singles.map((t) => t.id)).toEqual(['5']);
    expect(new Set(albums.map((g) => g.name)).size).toBe(albums.length);
  });

  it('opens the artist, not the release, and shows the cover', () => {
    expect(forkArtistOf(albums[0])).toBe('Polyphia');
    expect(forkArtistOf(groups[0])).toBe('Polyphia');
    expect(forkIsAlbumGroup(albums[0])).toBe(true);
    expect(forkIsAlbumGroup(groups[0])).toBe(false);
    const images = forkGroupImages(albums);
    expect(images.get('be not afraid')).toBe('bna.jpg');
    expect(images.get('with eyes to see')).toBe('With Eyes to See.jpg');
    expect(images.has('greatest hits — polyphia')).toBe(false);
  });

  it('filters by release or artist name, and by failing', () => {
    expect(forkFilterAlbumGroups(albums, 'eyes', false).map((g) => g.name)).toEqual([
      'With Eyes to See',
    ]);
    expect(forkFilterAlbumGroups(albums, 'other', false).map((g) => g.name)).toEqual([
      'Greatest Hits — Other',
    ]);
    expect(forkFilterAlbumGroups(albums, '', true).map((g) => g.name)).toEqual(['Be Not Afraid']);
  });
});
