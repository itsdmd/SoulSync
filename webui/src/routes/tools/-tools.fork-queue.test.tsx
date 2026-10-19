import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ForkFixQueue } from './-tools.fork-queue';
import type { FindingGroup } from './-tools.groups';

import * as api from './-tools.api';
import { forkClearGroup } from './-tools.fork-queue';
import { ForkFixQueueBar } from './-ui/fork-fix-queue-bar';

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

const item = (id: number, label: string) => ({
  id,
  finding_type: label,
  fix_action: null,
  label,
  count: 1200,
});

describe('fork fix queue bar', () => {
  it('lists the waiting entries and drops one on ×', () => {
    const remove = vi.fn();
    const queue = {
      state: { items: [item(1, 'Cover art'), item(2, 'Lyrics')], current: null, problems: [] },
      reload: vi.fn(),
      remove,
    } as unknown as ForkFixQueue;
    render(<ForkFixQueueBar queue={queue} />);
    expect(screen.getByText(/Cover art \(1,200\)/)).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText('Remove Lyrics from the queue'));
    expect(remove).toHaveBeenCalledWith(2);
    fireEvent.click(screen.getByText('Clear queue'));
    expect(remove).toHaveBeenCalledWith();
  });

  it('renders nothing when the queue is empty', () => {
    const queue = {
      state: { items: [], current: null, problems: [] },
      reload: vi.fn(),
      remove: vi.fn(),
    } as unknown as ForkFixQueue;
    const { container } = render(<ForkFixQueueBar queue={queue} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe('forkClearGroup', () => {
  const group = {
    finding_type: 'orphan_file',
    pending: 7,
    job_ids: ['j1', 'j2'],
  } as unknown as FindingGroup;

  it('deletes the pending rows of the type, job by job, after asking', async () => {
    vi.stubGlobal('showConfirmDialog', vi.fn().mockResolvedValue(true));
    const clear = vi.spyOn(api, 'clearFindings').mockResolvedValue({ success: true, deleted: 3 });
    expect(await forkClearGroup(group, 'Orphan files')).toBe(6);
    expect(clear).toHaveBeenCalledWith({
      jobId: 'j1',
      status: 'pending',
      findingType: 'orphan_file',
    });
    expect(clear).toHaveBeenCalledTimes(2);
  });

  it('does nothing when the prompt is declined', async () => {
    vi.stubGlobal('showConfirmDialog', vi.fn().mockResolvedValue(false));
    const clear = vi.spyOn(api, 'clearFindings');
    expect(await forkClearGroup(group, 'Orphan files')).toBeNull();
    expect(clear).not.toHaveBeenCalled();
  });
});
