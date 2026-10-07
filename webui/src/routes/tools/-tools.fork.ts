/**
 * fork (itsdmd/SoulSync): settings that only apply in some combinations.
 *
 * The Comma Artist Splitter either writes each artist as its own tag or joins
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
