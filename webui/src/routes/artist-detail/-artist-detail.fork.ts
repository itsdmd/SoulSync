import type { CompletionEvent } from './-artist-detail.completion';
import type { Discography } from './-artist-detail.types';

/**
 * fork (itsdmd/SoulSync): glue between the artist page and the fork's plain-JS
 * album tools (webui/static/fork-album.js). See FORK.md.
 */

/**
 * Sent by the album pop-up and the folder matcher when an album's library
 * analysis is done; `detail` is a completion event for that release. The
 * artist page repaints the card from it instead of waiting for the next visit.
 */
export const FORK_ALBUM_CHECKED = 'fork:album-checked';

export function albumCheckedEvent(event: Event): CompletionEvent | null {
  const detail = (event as CustomEvent<CompletionEvent | undefined>).detail;
  return detail && detail.id != null ? { ...detail, type: 'completion' } : null;
}

const TYPE_LABELS = { albums: 'Album', eps: 'EP', singles: 'Single' } as const;

export interface FolderMatchRequest {
  artist: { name: string };
  source: string;
  releases: { id: string; name: string; type: string }[];
}

/** Every release of the discography, in page order, for the folder matcher. */
export function folderMatchRequest(
  artistName: string | undefined,
  discography: Discography,
): FolderMatchRequest {
  const releases: FolderMatchRequest['releases'] = [];
  const seen = new Set<string>();
  for (const bucket of ['albums', 'eps', 'singles'] as const) {
    for (const release of discography[bucket] ?? []) {
      const id = String(release.id ?? '');
      // gap-fill cards belong to another source: their ids mean nothing here
      if (!id || seen.has(id) || release._gap_source) continue;
      seen.add(id);
      releases.push({ id, name: String(release.title ?? ''), type: TYPE_LABELS[bucket] });
    }
  }
  return { artist: { name: artistName ?? '' }, source: String(discography.source ?? ''), releases };
}

/** Opens the matcher; undefined when the fork's script is not loaded. */
export function forkFolderMatcher(): ((request: FolderMatchRequest) => void) | undefined {
  const open = (window as unknown as { forkMatchAlbumFolders?: (r: FolderMatchRequest) => void })
    .forkMatchAlbumFolders;
  return typeof open === 'function' ? open : undefined;
}
