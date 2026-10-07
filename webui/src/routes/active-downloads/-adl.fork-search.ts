import type { AdlBatch, AdlBatchHistoryEntry, AdlDownload } from './-adl.types';

/**
 * fork (itsdmd/SoulSync): the Downloads page search box.
 *
 * A batch is kept when its own name matches, or when any track in it matches
 * by song title, artist or album. Every word typed has to appear somewhere in
 * the same thing (a batch name, or one track's title + artist + album), in any
 * order; case and accents are ignored.
 */

export function normalizeSearch(text: unknown): string {
  return String(text ?? '')
    .normalize('NFKD')
    .replace(/[̀-ͯ]/g, '')
    .toLowerCase();
}

export function searchTerms(query: string): string[] {
  return normalizeSearch(query).split(/\s+/).filter(Boolean);
}

function matches(haystack: string, terms: string[]): boolean {
  return terms.every((term) => haystack.includes(term));
}

function rowText(row: AdlDownload): string {
  return normalizeSearch(`${row.title} ${row.artist} ${row.album}`);
}

export interface AdlSearchResult {
  /** Status-filtered rows that survive the search. */
  rows: AdlDownload[];
  /** Every row that survives the search, ignoring the status chip. */
  allRows: AdlDownload[];
  batches: AdlBatch[];
  history: AdlBatchHistoryEntry[];
}

export function filterDownloads(
  query: string,
  input: {
    rows: AdlDownload[];
    allRows: AdlDownload[];
    batches: AdlBatch[];
    history: AdlBatchHistoryEntry[];
  },
): AdlSearchResult {
  const terms = searchTerms(query);
  if (terms.length === 0) return input;

  // A batch whose NAME matches keeps all of its tracks.
  const namedBatchIds = new Set(
    input.batches
      .filter((batch) => matches(normalizeSearch(batch.batch_name), terms))
      .map((batch) => batch.batch_id),
  );
  const keepRow = (row: AdlDownload) =>
    (row.batch_id && namedBatchIds.has(row.batch_id)) ||
    matches(normalizeSearch(row.batch_name), terms) ||
    matches(rowText(row), terms);

  const allRows = input.allRows.filter(keepRow);
  // Matched against ALL of a batch's tracks, so a batch stays findable by a
  // song the status chip is currently hiding.
  const batchIdsWithMatch = new Set(allRows.map((row) => row.batch_id).filter(Boolean));

  return {
    rows: input.rows.filter(keepRow),
    allRows,
    batches: input.batches.filter(
      (batch) => namedBatchIds.has(batch.batch_id) || batchIdsWithMatch.has(batch.batch_id),
    ),
    history: input.history.filter((entry) => matches(normalizeSearch(entry.playlist_name), terms)),
  };
}
