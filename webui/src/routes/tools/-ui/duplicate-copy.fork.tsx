/**
 * fork (itsdmd/SoulSync): what a duplicate-track copy shows beyond the
 * upstream line (album, bitrate, length, path): its size and format, and an
 * explicit button to keep exactly this copy. Clicking the row still works.
 */
export interface DuplicateCopyInfo {
  file_size?: number;
  format?: string;
  file_found?: boolean;
  file_path?: string;
}

export function formatFileSize(bytes: number): string {
  if (bytes >= 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${bytes} B`;
}

export function DuplicateCopyExtras({
  track,
  onKeep,
}: {
  track: DuplicateCopyInfo;
  onKeep: () => void;
}) {
  const parts: string[] = [];
  if (track.format) parts.push(track.format);
  if (typeof track.file_size === 'number') parts.push(formatFileSize(track.file_size));
  else if (track.file_found === false) parts.push('file not found on disk');
  return (
    <>
      {parts.length ? <span>{parts.join(' · ')}</span> : null}
      <button
        className="btn btn--sm btn--secondary"
        type="button"
        onClick={(event) => {
          event.stopPropagation();
          onKeep();
        }}
      >
        Keep this version
      </button>
    </>
  );
}
