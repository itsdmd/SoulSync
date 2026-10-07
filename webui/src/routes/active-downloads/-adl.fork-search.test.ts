import { describe, expect, it } from 'vitest';

import type { AdlBatch, AdlBatchHistoryEntry, AdlDownload } from './-adl.types';

import { filterDownloads, searchTerms } from './-adl.fork-search';

/** fork (itsdmd/SoulSync): the Downloads page search box. */

const row = (over: Partial<AdlDownload>): AdlDownload =>
  ({
    task_id: `${over.batch_id ?? 'none'}-${over.title}`,
    title: '',
    artist: '',
    album: '',
    status: 'queued',
    batch_id: '',
    batch_name: '',
    ...over,
  }) as AdlDownload;

const batch = (batch_id: string, batch_name: string): AdlBatch =>
  ({ batch_id, batch_name, phase: 'downloading' }) as AdlBatch;

const batches = [
  batch('b1', "November's Chopin (十一月的蕭邦)"),
  batch('b2', 'Björk — Post'),
  batch('b3', 'Liked Songs'),
];
const allRows = [
  row({ batch_id: 'b1', title: 'Nocturne (夜曲)', artist: 'Jay Chou', album: "November's Chopin" }),
  row({ batch_id: 'b1', title: 'Hair Like Snow', artist: 'Jay Chou', album: "November's Chopin" }),
  row({ batch_id: 'b2', title: 'Army of Me', artist: 'Björk', album: 'Post', status: 'completed' }),
  row({ batch_id: 'b2', title: 'Hyperballad', artist: 'Björk', album: 'Post' }),
  row({ batch_id: 'b3', title: 'Creep', artist: 'Radiohead', album: 'Pablo Honey' }),
  row({ batch_id: 'b3', title: 'Yellow', artist: 'Coldplay', album: 'Parachutes' }),
  row({ title: 'Loose Track', artist: 'Nobody', album: 'Old Import', batch_name: 'Soulseek' }),
];
const history: AdlBatchHistoryEntry[] = [
  { playlist_name: 'Post' },
  { playlist_name: 'Discover Weekly' },
];
const input = { rows: allRows, allRows, batches, history };
const ids = (list: AdlBatch[]) => list.map((b) => b.batch_id);
const titles = (list: AdlDownload[]) => list.map((r) => r.title);

describe('downloads search', () => {
  it('returns everything untouched for an empty query', () => {
    expect(filterDownloads('   ', input)).toBe(input);
  });

  it('finds a batch by its album name and keeps all of its tracks', () => {
    const found = filterDownloads('chopin', input);
    expect(ids(found.batches)).toEqual(['b1']);
    expect(titles(found.rows)).toEqual(['Nocturne (夜曲)', 'Hair Like Snow']);
  });

  it('finds a batch by a song in it and shows only the matching songs', () => {
    const found = filterDownloads('creep', input);
    expect(ids(found.batches)).toEqual(['b3']);
    expect(titles(found.rows)).toEqual(['Creep']);
  });

  it('finds by artist across batches', () => {
    const found = filterDownloads('coldplay', input);
    expect(ids(found.batches)).toEqual(['b3']);
    expect(titles(found.rows)).toEqual(['Yellow']);
  });

  it('ignores case and accents, and matches CJK text', () => {
    expect(ids(filterDownloads('BJORK', input).batches)).toEqual(['b2']);
    expect(titles(filterDownloads('夜曲', input).rows)).toEqual(['Nocturne (夜曲)']);
  });

  it('needs every word, in any order, within one track', () => {
    expect(titles(filterDownloads('honey radiohead', input).rows)).toEqual(['Creep']);
    // "creep" and "coldplay" are different tracks of the same batch: no match
    expect(filterDownloads('creep coldplay', input).rows).toEqual([]);
    expect(searchTerms('  Jay   CHOU ')).toEqual(['jay', 'chou']);
  });

  it('keeps batchless rows that match, including by their batch name', () => {
    expect(titles(filterDownloads('old import', input).rows)).toEqual(['Loose Track']);
    expect(titles(filterDownloads('soulseek', input).rows)).toEqual(['Loose Track']);
    expect(filterDownloads('old import', input).batches).toEqual([]);
  });

  it('keeps a batch findable by a song the status chip is hiding', () => {
    const queuedOnly = allRows.filter((r) => r.status === 'queued');
    const found = filterDownloads('army of me', { ...input, rows: queuedOnly });
    expect(ids(found.batches)).toEqual(['b2']);
    expect(found.rows).toEqual([]);
    expect(titles(found.allRows)).toEqual(['Army of Me']);
  });

  it('filters recent history by name', () => {
    expect(filterDownloads('post', input).history).toEqual([{ playlist_name: 'Post' }]);
  });
});
