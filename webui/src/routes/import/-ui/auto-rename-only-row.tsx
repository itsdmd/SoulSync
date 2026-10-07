import { useEffect, useState } from 'react';

import { Switch } from '@/components/form/form';

import styles from './import-page.module.css';

/**
 * fork (itsdmd/SoulSync): "Rename only" for the automatic import watcher.
 * Stored in the fork's settings (/api/fork/settings) and saved the moment it
 * is flipped, independently of the drawer's own Save.
 */
export function AutoRenameOnlyRow({ open }: { open: boolean }) {
  const [value, setValue] = useState<boolean | null>(null);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    // a signal opts out of the app's 2.5s GET sharing, so this is never a stale read
    void fetch('/api/fork/settings', { signal: new AbortController().signal })
      .then((response) => response.json())
      .then((data) => {
        if (!cancelled) setValue(Boolean(data?.settings?.import?.rename_only_auto));
      })
      .catch(() => {
        if (!cancelled) setValue(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  const change = async (next: boolean) => {
    const previous = value;
    setValue(next);
    try {
      const response = await fetch('/api/fork/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ import: { rename_only_auto: next } }),
      });
      if (!response.ok) throw new Error(`Save failed (${response.status})`);
      window.showToast?.(
        next ? 'Watcher will rename only' : 'Watcher will tag as usual',
        'success',
      );
    } catch (error) {
      setValue(previous);
      window.showToast?.((error as Error).message, 'error');
    }
  };

  return (
    <div className={styles.settingRow}>
      <div>
        <div className={styles.settingLabel}>Rename only</div>
        <div className={styles.settingHelp}>
          The watcher moves and renames files to your path format and leaves their tags, artwork and
          audio exactly as they are.
        </div>
      </div>
      <div className={styles.settingControl}>
        <Switch
          checked={value === true}
          disabled={value === null}
          aria-label="Rename only (automatic import)"
          onCheckedChange={(checked) => void change(Boolean(checked))}
        />
      </div>
    </div>
  );
}
