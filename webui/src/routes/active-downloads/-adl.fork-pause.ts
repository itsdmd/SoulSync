import { useCallback, useEffect, useState } from 'react';

import { apiClient, readJson } from '@/app/api-client';

/**
 * fork (itsdmd/SoulSync): Pause All / Resume All on the Downloads page.
 *
 * Paused means the server starts no queued download; what is already
 * searching or transferring runs to its end. The server keeps the switch in
 * memory only, so a restart of SoulSync resumes by itself. See FORK.md.
 */

interface PauseState {
  success?: boolean;
  error?: string;
  paused?: boolean;
}

const POLL_MS = 15000;

export async function forkFetchDownloadsPaused(): Promise<boolean> {
  const data = await readJson<PauseState>(apiClient.get('fork/downloads/pause'));
  return data.paused === true;
}

export async function forkSetDownloadsPaused(paused: boolean): Promise<boolean> {
  const data = await readJson<PauseState>(
    apiClient.post('fork/downloads/pause', { json: { paused } }),
  );
  if (data.success === false) throw new Error(data.error || 'Failed');
  return data.paused === true;
}

interface DiscardResult {
  success?: boolean;
  error?: string;
  discarded_batches?: number;
  discarded_tasks?: number;
}

/** Stop every unfinished batch and drop it; nothing goes to the wishlist. */
export async function forkDiscardAllDownloads(): Promise<DiscardResult> {
  const data = await readJson<DiscardResult>(apiClient.post('fork/downloads/discard-all'));
  if (data.success === false) throw new Error(data.error || 'Failed');
  return data;
}

export interface ForkDownloadsPause {
  paused: boolean;
  pending: boolean;
  toggle: () => void;
  discardPending: boolean;
  /** Ask first, then discard every unfinished download (no wishlist). */
  discardAll: () => void;
}

/** The switch, re-read now and then so another tab's change shows up. */
export function useForkDownloadsPause(onChanged?: () => void): ForkDownloadsPause {
  const [paused, setPaused] = useState(false);
  const [pending, setPending] = useState(false);
  const [discardPending, setDiscardPending] = useState(false);

  useEffect(() => {
    let alive = true;
    const read = () =>
      forkFetchDownloadsPaused()
        .then((value) => {
          if (alive) setPaused(value);
        })
        .catch(() => {});
    void read();
    const timer = window.setInterval(() => void read(), POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, []);

  const toggle = useCallback(() => {
    if (pending) return;
    const next = !paused;
    setPending(true);
    forkSetDownloadsPaused(next)
      .then((value) => {
        setPaused(value);
        window.showToast?.(
          value
            ? 'Downloads paused — queued tracks wait, running ones finish'
            : 'Downloads resumed',
          'info',
        );
        onChanged?.();
      })
      .catch((error: Error) =>
        window.showToast?.(`Could not ${next ? 'pause' : 'resume'}: ${error.message}`, 'error'),
      )
      .finally(() => setPending(false));
  }, [onChanged, paused, pending]);

  const discardAll = useCallback(() => {
    if (discardPending) return;
    void (async () => {
      const confirmed = await window.showConfirmDialog?.({
        title: 'Discard All Downloads',
        message:
          'Stop every active and queued download and remove it from the list? Unlike Cancel All, nothing is added to the wishlist.',
        confirmText: 'Discard All',
        destructive: true,
      });
      if (!confirmed) return;
      setDiscardPending(true);
      try {
        const result = await forkDiscardAllDownloads();
        window.showToast?.(
          `Discarded ${result.discarded_tasks ?? 0} downloads in ${result.discarded_batches ?? 0} batches`,
          'success',
        );
        onChanged?.();
      } catch (error) {
        window.showToast?.(`Could not discard: ${(error as Error).message}`, 'error');
      } finally {
        setDiscardPending(false);
      }
    })();
  }, [discardPending, onChanged]);

  return { paused, pending, toggle, discardPending, discardAll };
}
