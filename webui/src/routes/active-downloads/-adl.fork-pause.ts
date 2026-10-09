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

export interface ForkDownloadsPause {
  paused: boolean;
  pending: boolean;
  toggle: () => void;
}

/** The switch, re-read now and then so another tab's change shows up. */
export function useForkDownloadsPause(onChanged?: () => void): ForkDownloadsPause {
  const [paused, setPaused] = useState(false);
  const [pending, setPending] = useState(false);

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

  return { paused, pending, toggle };
}
