import { useCallback, useEffect, useRef, useState } from 'react';

import { apiClient, readJson } from '@/app/api-client';

import type { FindingGroup } from './-tools.groups';

import { clearFindings } from './-tools.api';

/**
 * fork (itsdmd/SoulSync): the fix queue on the Tools page findings, and
 * "Clear" for a whole group.
 *
 * "Add to queue" asks the same questions as the Fix all button, then hands the
 * answers to the server, which starts each queued entry once the running job
 * is done. Only the groups you add are queued. See FORK.md.
 */

export interface ForkQueueItem {
  id: number;
  finding_type: string;
  fix_action: string | null;
  label: string;
  count: number;
}

export interface ForkQueueProblem extends ForkQueueItem {
  error: string;
}

export interface ForkQueueState {
  items: ForkQueueItem[];
  current: (ForkQueueItem & { total?: number }) | null;
  problems: ForkQueueProblem[];
}

const EMPTY: ForkQueueState = { items: [], current: null, problems: [] };
const POLL_MS = 3000;

export async function forkFetchFixQueue(): Promise<ForkQueueState> {
  const data = await readJson<Partial<ForkQueueState>>(apiClient.get('fork/findings/queue'));
  return {
    items: data.items ?? [],
    current: data.current ?? null,
    problems: data.problems ?? [],
  };
}

export async function forkQueueFix(entry: {
  findingType: string;
  fixAction: string | null;
  label: string;
  count: number;
}): Promise<{ added?: boolean; duplicate?: boolean; position?: number; error?: string }> {
  return readJson(
    apiClient.post('fork/findings/queue', {
      json: {
        finding_type: entry.findingType,
        fix_action: entry.fixAction,
        label: entry.label,
        count: entry.count,
      },
      throwHttpErrors: false,
    }),
  );
}

export async function forkUnqueueFix(id?: number): Promise<void> {
  await readJson(
    apiClient.post('fork/findings/queue/remove', { json: id === undefined ? {} : { id } }),
  );
}

export interface ForkFixQueue {
  state: ForkQueueState;
  /** Re-read now (after adding or removing). */
  reload: () => Promise<void>;
  remove: (id?: number) => Promise<void>;
}

/**
 * The queue as the server holds it. Re-read while it has entries, and when an
 * entry has started a job this page is not yet watching, `onStarted` lets the
 * page pick the progress bar up.
 */
export function useForkFixQueue(onStarted: () => void): ForkFixQueue {
  const [state, setState] = useState<ForkQueueState>(EMPTY);
  const startedRef = useRef(onStarted);
  startedRef.current = onStarted;
  const lastCurrent = useRef<number | null>(null);

  const reload = useCallback(async () => {
    try {
      const next = await forkFetchFixQueue();
      setState(next);
      const id = next.current?.id ?? null;
      if (id !== null && id !== lastCurrent.current) startedRef.current();
      lastCurrent.current = id;
    } catch {
      /* transient — the next poll tries again */
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const active = state.items.length > 0;
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => void reload(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [active, reload]);

  const remove = useCallback(
    async (id?: number) => {
      await forkUnqueueFix(id);
      await reload();
    },
    [reload],
  );

  return { state, reload, remove };
}

/**
 * Delete a group's open findings without ignoring them: the next scan raises
 * whatever is still wrong. For after something that changed the files under
 * the findings (a reorganize, say), when the old results no longer line up.
 */
export async function forkClearGroup(group: FindingGroup, label: string): Promise<number | null> {
  const count = group.pending;
  const confirmed = await window.showConfirmDialog?.({
    title: `Clear ${label}`,
    message: `Delete the ${count.toLocaleString()} open ${label.toLowerCase()} findings? They are not ignored: the next scan raises them again if they are still true.`,
    confirmText: 'Clear',
    destructive: false,
  });
  if (!confirmed) return null;
  // a type raised by several jobs is cleared job by job
  const jobIds = group.job_ids?.length ? group.job_ids : [undefined];
  let deleted = 0;
  for (const jobId of jobIds) {
    const part = await clearFindings({ jobId, status: 'pending', findingType: group.finding_type });
    deleted += part.deleted || 0;
  }
  return deleted;
}
