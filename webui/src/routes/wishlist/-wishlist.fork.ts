// fork (itsdmd/SoulSync): the Wishlist page can group by album instead of by
// artist. An album group has the same shape as an artist group — one album
// (and/or the singles of one release) — so the nebula and the list render it
// unchanged; `forkArtist` carries the artist it belongs to. See FORK.md.
import { apiClient, readJson } from '@/app/api-client';

import type { ParsedWishlistTrack, WishlistArtistGroup } from './-wishlist.types';

export type WishlistGroupBy = 'artist' | 'album';

export interface ForkAlbumGroup extends WishlistArtistGroup {
  /** The artist the release belongs to; `name` is the release. */
  forkArtist: string;
  forkImage: string;
}

const STORAGE_KEY = 'forkWishlistGroupBy';

export function forkLoadGroupBy(): WishlistGroupBy {
  try {
    return window.localStorage.getItem(STORAGE_KEY) === 'album' ? 'album' : 'artist';
  } catch {
    return 'artist';
  }
}

export function forkSaveGroupBy(value: WishlistGroupBy): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, value);
  } catch {
    /* private mode — the choice still holds for the session */
  }
}

/**
 * One group per release, busiest first. Album tracks and singles that share
 * an artist and a release name land in the same group. Two artists' releases
 * of the same name stay apart, told apart by "Name — Artist".
 */
export function forkGroupByAlbum(groups: WishlistArtistGroup[]): ForkAlbumGroup[] {
  const byRelease = new Map<string, ForkAlbumGroup>();
  const ensure = (artist: string, title: string, image: string) => {
    const key = `${artist}\u0000${title}`;
    let entry = byRelease.get(key);
    if (!entry) {
      entry = {
        name: title,
        forkArtist: artist,
        forkImage: image,
        albums: [],
        singles: [],
        total: 0,
        failingCount: 0,
      };
      byRelease.set(key, entry);
    }
    if (!entry.forkImage && image) entry.forkImage = image;
    return entry;
  };
  const count = (entry: ForkAlbumGroup, tracks: ParsedWishlistTrack[]) => {
    entry.total += tracks.length;
    entry.failingCount += tracks.filter((track) => track.failing).length;
  };

  for (const group of groups) {
    for (const album of group.albums) {
      const entry = ensure(group.name, album.name, album.image);
      entry.albums.push(album);
      count(entry, album.tracks);
    }
    for (const single of group.singles) {
      const entry = ensure(group.name, single.album, single.image);
      entry.singles.push(single);
      count(entry, [single]);
    }
  }

  const out = [...byRelease.values()];
  const titles = new Map<string, number>();
  for (const entry of out) {
    const title = entry.name.toLowerCase();
    titles.set(title, (titles.get(title) ?? 0) + 1);
  }
  for (const entry of out) {
    if ((titles.get(entry.name.toLowerCase()) ?? 0) > 1) {
      entry.name = `${entry.name} — ${entry.forkArtist}`;
    }
  }
  return out.sort((a, b) => b.total - a.total);
}

/** The artist to open for a group: itself, or the artist of an album group. */
export function forkArtistOf(group: WishlistArtistGroup): string {
  return (group as Partial<ForkAlbumGroup>).forkArtist ?? group.name;
}

export function forkIsAlbumGroup(group: WishlistArtistGroup): boolean {
  return typeof (group as Partial<ForkAlbumGroup>).forkArtist === 'string';
}

/** Group name (lower-cased) -> cover, in the shape the views expect for artist photos. */
export function forkGroupImages(groups: ForkAlbumGroup[]): Map<string, string> {
  const map = new Map<string, string>();
  for (const group of groups) {
    if (group.forkImage) map.set(group.name.toLowerCase(), group.forkImage);
  }
  return map;
}

/** The search box and the Failing chip, for album groups: release or artist name. */
export function forkFilterAlbumGroups(
  groups: ForkAlbumGroup[],
  query: string,
  failingOnly: boolean,
): ForkAlbumGroup[] {
  const needle = query.toLowerCase().trim();
  return groups.filter((group) => {
    if (failingOnly && group.failingCount === 0) return false;
    if (!needle) return true;
    return (
      group.name.toLowerCase().includes(needle) || group.forkArtist.toLowerCase().includes(needle)
    );
  });
}

/**
 * Remove the ticked tracks from the wishlist for good — unlike "Skip", which
 * also puts them on the ignore list. Returns how many were removed.
 */
export async function forkRemoveWishlistTracks(trackIds: string[]): Promise<number> {
  const payload = await readJson<{ success?: boolean; error?: string; removed?: number }>(
    apiClient.post('wishlist/remove-batch', { json: { spotify_track_ids: trackIds } }),
  );
  if (payload.success === false) throw new Error(payload.error || 'Failed');
  return payload.removed ?? 0;
}
