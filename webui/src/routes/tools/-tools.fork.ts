/**
 * fork (itsdmd/SoulSync): settings that only apply in some combinations.
 *
 * The Multiple Artist Formatter (job id comma_artist_splitter) either writes each artist as its own tag or joins
 * them with a chosen separator — never both — so the separator settings are
 * locked while tag splitting is on, and the custom separator is only open when
 * Separator is set to Custom.
 */
type Values = Record<string, unknown>;

function isOn(value: unknown): boolean {
  return value === true || (typeof value === 'string' && value.toLowerCase() === 'true');
}

const SPLIT_ON = 'Not used while Split Into Separate Tags is on: each artist gets its own tag.';

const LOCKS: Record<string, Record<string, (values: Values) => string | undefined>> = {
  comma_artist_splitter: {
    separator: (values) => (isOn(values.split_into_separate_tags) ? SPLIT_ON : undefined),
    custom_separator: (values) =>
      isOn(values.split_into_separate_tags)
        ? SPLIT_ON
        : String(values.separator ?? '').toLowerCase() !== 'custom'
          ? 'Set Separator to Custom to use your own character.'
          : undefined,
  },
};

/** fork: fix-button labels for the fork's own finding types. */
const FORK_FIX_LABELS: Record<string, string> = {
  fork_untranslated: 'Apply',
  fork_album_volumes: 'Group',
};

export function forkFixLabel(findingType: string): string | null {
  return FORK_FIX_LABELS[findingType] ?? null;
}

/**
 * fork: findings whose content can be edited before fixing. The editor itself
 * is plain JS (webui/static/fork-album.js); this only finds it. It announces a
 * saved change with FORK_FINDINGS_CHANGED so the list reloads.
 */
export const FORK_FINDINGS_CHANGED = 'fork:findings-changed';

type EditableFinding = { finding_type: string; status: string };

export function forkFindingEditor(finding: EditableFinding): (() => void) | undefined {
  if (finding.finding_type !== 'fork_album_volumes' || finding.status !== 'pending')
    return undefined;
  const open = (window as unknown as { forkEditVolumeGroup?: (f: EditableFinding) => void })
    .forkEditVolumeGroup;
  return typeof open === 'function' ? () => open(finding) : undefined;
}

/** Why a setting cannot be edited right now, or undefined when it can. */
export function repairSettingLockReason(
  jobId: string,
  key: string,
  values: Values,
): string | undefined {
  return LOCKS[jobId]?.[key]?.(values);
}

export function isRepairSettingLocked(jobId: string, key: string, values: Values): boolean {
  return repairSettingLockReason(jobId, key, values) !== undefined;
}
