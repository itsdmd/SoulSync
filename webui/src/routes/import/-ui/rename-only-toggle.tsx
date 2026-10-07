import { Switch } from '@/components/form/form';

import { useRenameOnlyStore } from '../-import.fork';

/** fork (itsdmd/SoulSync): header switch for rename-only imports. */
export function RenameOnlyToggle() {
  const renameOnly = useRenameOnlyStore((state) => state.renameOnly);
  const setRenameOnly = useRenameOnlyStore((state) => state.setRenameOnly);
  return (
    <label
      id="import-rename-only"
      title="Move and rename files to your path format without changing their tags, artwork or audio"
      style={{ display: 'inline-flex', alignItems: 'center', gap: 8, cursor: 'pointer' }}
    >
      <Switch
        checked={renameOnly}
        aria-label="Rename only"
        onCheckedChange={(checked) => setRenameOnly(Boolean(checked))}
      />
      <span>Rename only</span>
    </label>
  );
}
