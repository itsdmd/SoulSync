import type { ImportInboxItem, ImportInboxPayload } from './-import.types';

/**
 * fork (itsdmd/SoulSync): which inbox item the matcher page is about.
 *
 * Two things made the page say "This item is no longer in the import folder"
 * for a folder that was still there:
 *
 * 1. While the import folder is being re-read the inbox answers
 *    `{ scanning: true }` with NO items. That happens after every imported
 *    track (the scan cache is dropped), so during a long import any matcher
 *    page — including one for a different folder — briefly saw an empty inbox.
 * 2. An item's key is a hash of its files. Importing a folder moves its files
 *    out one at a time, so its key changes after every track.
 *
 * So: keep the last item we saw while a re-read is in flight, and when the key
 * is gone look the item up again by its folder path.
 */
export type MatcherItemState =
  | { state: 'ready'; item: ImportInboxItem }
  /** the folder is being re-read and we have nothing to show yet */
  | { state: 'reading' }
  | { state: 'gone' };

export function resolveMatcherItem(
  payload: ImportInboxPayload | undefined,
  itemKey: string,
  lastSeen: ImportInboxItem | null,
): MatcherItemState {
  const items = payload?.items;
  if (!items) {
    // no list at all: a re-read in flight (or the first load) — never "gone"
    if (lastSeen) return { state: 'ready', item: lastSeen };
    return payload?.scanning || !payload ? { state: 'reading' } : { state: 'gone' };
  }
  const byKey = items.find((row) => row.key === itemKey);
  if (byKey) return byKey.in_staging ? { state: 'ready', item: byKey } : { state: 'gone' };
  if (lastSeen?.folder_path) {
    const byFolder = items.find(
      (row) => row.in_staging && row.folder_path === lastSeen.folder_path,
    );
    if (byFolder) return { state: 'ready', item: byFolder };
  }
  return { state: 'gone' };
}

/** Poll while the folder is being re-read, as the inbox list does. */
export function matcherRefetchInterval(payload: ImportInboxPayload | undefined): number | false {
  return payload?.scanning ? 1500 : false;
}
