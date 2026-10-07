/**
 * fork (itsdmd/SoulSync): search box above the batch list. Filters by album /
 * batch name, song title and artist — see ../-adl.fork-search.
 */
export function AdlSearchBar({
  value,
  onChange,
  resultText,
}: {
  value: string;
  onChange: (value: string) => void;
  /** e.g. "3 batches · 14 tracks"; shown only while a query is active. */
  resultText: string;
}) {
  return (
    <div className="adl-search" id="adl-search">
      <svg
        className="adl-search-icon"
        width="15"
        height="15"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        aria-hidden="true"
      >
        <circle cx="11" cy="11" r="7" />
        <line x1="21" y1="21" x2="16.65" y2="16.65" />
      </svg>
      <input
        type="search"
        className="adl-search-input"
        id="adl-search-input"
        placeholder="Filter by album, song or artist…"
        aria-label="Filter downloads by album, song or artist"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === 'Escape') onChange('');
        }}
      />
      {value ? (
        <>
          <span className="adl-search-count" aria-live="polite">
            {resultText}
          </span>
          <button
            type="button"
            className="adl-search-clear"
            title="Clear search"
            aria-label="Clear search"
            onClick={() => onChange('')}
          >
            ×
          </button>
        </>
      ) : null}
    </div>
  );
}
