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
