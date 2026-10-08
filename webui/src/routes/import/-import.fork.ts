import { create } from 'zustand';

/**
 * fork (itsdmd/SoulSync): the Import page's "Rename only" switch.
 *
 * On, a manual import still moves and renames the file to the configured path
 * format, using the release it was matched to — but the file itself is left
 * exactly as it was: no tag rewrite, no cover or lyrics embed, no ReplayGain,
 * no conversion. Remembered per browser.
 */
const STORAGE_KEY = 'soulsync-fork.import.rename-only';

function readStored(): boolean {
  try {
    return window.localStorage.getItem(STORAGE_KEY) === '1';
  } catch {
    return false;
  }
}

export const useRenameOnlyStore = create<{
  renameOnly: boolean;
  setRenameOnly: (value: boolean) => void;
}>((set) => ({
  renameOnly: readStored(),
  setRenameOnly: (value) => {
    try {
      window.localStorage.setItem(STORAGE_KEY, value ? '1' : '0');
    } catch {
      // storage blocked: the switch still works for this page load
    }
    set({ renameOnly: value });
  },
}));

/** Read at request time by the import-process calls. */
export function getRenameOnly(): boolean {
  return useRenameOnlyStore.getState().renameOnly;
}

/**
 * fork: dismissing a selection. Upstream only dismisses entries the importer
 * has a record for (needs review / needs identifying); a fresh drop that is
 * still waiting, or a failed one, could not be cleared from a selection.
 */
interface DismissableItem {
  key: string;
  status: string;
  in_staging: boolean;
  history_id?: number | null;
  folder_path: string;
  folder_name: string;
  file_count: number;
}

const DISMISSABLE = new Set(['waiting', 'needs_review', 'needs_identify', 'failed']);

export function forkCanDismiss(item: DismissableItem): boolean {
  return item.in_staging && DISMISSABLE.has(item.status);
}

export async function forkDismissItems(items: DismissableItem[]): Promise<number> {
  const response = await fetch('/api/fork/import/dismiss', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      items: items.map((item) => ({
        key: item.key,
        history_id: item.history_id ?? null,
        folder_path: item.folder_path,
        folder_name: item.folder_name,
        file_count: item.file_count,
      })),
    }),
  });
  const payload = (await response.json().catch(() => ({}))) as {
    success?: boolean;
    dismissed?: number;
    errors?: string[];
    error?: string;
  };
  if (!response.ok || !payload.success) throw new Error(payload.error || 'Failed to dismiss');
  if (payload.errors?.length) throw new Error(`Some were not dismissed: ${payload.errors[0]}`);
  return payload.dismissed ?? 0;
}

/**
 * fork: manual import. The user edits the tags (and cover) of an entry's
 * files in a dialog (webui/static/fork-import.js) and SoulSync files the
 * tracks by them. The dialog announces FORK_IMPORT_CHANGED when the import
 * folder changed, so the inbox reloads.
 */
export const FORK_IMPORT_CHANGED = 'fork:import-changed';

interface ManualItem extends DismissableItem {
  name: string;
  files: { full_path: string }[];
}

export function forkCanManualImport(item: ManualItem): boolean {
  return forkCanDismiss(item) && item.files.length > 0;
}

type ManualImportOpener = (options: { entries: { name: string; paths: string[] }[] }) => void;

/** Opens the dialog for these entries, one after another; false when the fork's script is missing. */
export function forkManualImport(items: ManualItem[]): boolean {
  const open = (window as unknown as { forkManualImport?: ManualImportOpener }).forkManualImport;
  if (typeof open !== 'function') return false;
  open({
    entries: items.map((item) => ({
      name: item.name || item.folder_name,
      paths: item.files.map((file) => file.full_path),
    })),
  });
  return true;
}
