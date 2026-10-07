import { describe, expect, it } from 'vitest';

import type { ImportInboxItem, ImportInboxPayload } from './-import.types';

import { matcherRefetchInterval, resolveMatcherItem } from './-import.fork-matcher';

/** fork (itsdmd/SoulSync): the matcher must not call a folder "gone" while it is being re-read. */
const item = (key: string, folder: string, inStaging = true) =>
  ({
    key,
    folder_path: folder,
    in_staging: inStaging,
    kind: 'album',
  }) as unknown as ImportInboxItem;

const lanterns = item('k-lanterns', '/import/When Lanterns Echo the Moon');
const welkin = item('k-welkin-97', '/import/Song of the Welkin Moon');
const list = (...items: ImportInboxItem[]) => ({ success: true, items }) as ImportInboxPayload;
const scanning = { success: true, scanning: true } as ImportInboxPayload;

describe('matcher item during a re-read', () => {
  it('finds the item by key', () => {
    expect(resolveMatcherItem(list(lanterns, welkin), 'k-lanterns', null)).toEqual({
      state: 'ready',
      item: lanterns,
    });
  });

  it('waits, rather than saying gone, when the inbox is being re-read', () => {
    // opened straight into a scan: nothing seen yet
    expect(resolveMatcherItem(scanning, 'k-lanterns', null)).toEqual({ state: 'reading' });
    expect(resolveMatcherItem(undefined, 'k-lanterns', null)).toEqual({ state: 'reading' });
    // already showing the item: keep showing it through the scan
    expect(resolveMatcherItem(scanning, 'k-lanterns', lanterns)).toEqual({
      state: 'ready',
      item: lanterns,
    });
    expect(matcherRefetchInterval(scanning)).toBe(1500);
    expect(matcherRefetchInterval(list(lanterns))).toBe(false);
  });

  it('follows an item whose key changed because its files are being imported', () => {
    const fewerFiles = item('k-welkin-96', '/import/Song of the Welkin Moon');
    expect(resolveMatcherItem(list(lanterns, fewerFiles), 'k-welkin-97', welkin)).toEqual({
      state: 'ready',
      item: fewerFiles,
    });
  });

  it('still reports an item that really left the folder', () => {
    expect(resolveMatcherItem(list(lanterns), 'k-welkin-97', welkin)).toEqual({ state: 'gone' });
    expect(resolveMatcherItem(list(lanterns), 'k-unknown', null)).toEqual({ state: 'gone' });
    // a history-only row (files already imported) is not matchable
    const done = item('k-welkin-97', '/import/Song of the Welkin Moon', false);
    expect(resolveMatcherItem(list(done), 'k-welkin-97', welkin)).toEqual({ state: 'gone' });
    // a failed inbox response with no scan in progress
    expect(resolveMatcherItem({ success: false } as ImportInboxPayload, 'k', null)).toEqual({
      state: 'gone',
    });
  });
});
