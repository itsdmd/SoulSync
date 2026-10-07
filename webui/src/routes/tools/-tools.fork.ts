/**
 * fork (itsdmd/SoulSync): settings that only make sense while another one is
 * off. The Comma Artist Splitter can either write each artist as its own tag
 * or join them with a chosen separator — never both — so the separator is
 * locked while tag splitting is on.
 */
const LOCKED_WHILE_ON: Record<string, Record<string, { by: string; reason: string }>> = {
  comma_artist_splitter: {
    separator: {
      by: 'split_into_separate_tags',
      reason: 'Not used while Split Into Separate Tags is on: each artist gets its own tag.',
    },
  },
};

function isOn(value: unknown): boolean {
  return value === true || (typeof value === 'string' && value.toLowerCase() === 'true');
}

export function isRepairSettingLocked(
  jobId: string,
  key: string,
  values: Record<string, unknown>,
): boolean {
  const rule = LOCKED_WHILE_ON[jobId]?.[key];
  return Boolean(rule && isOn(values[rule.by]));
}

export function repairSettingLockReason(
  jobId: string,
  key: string,
  values: Record<string, unknown>,
): string | undefined {
  return isRepairSettingLocked(jobId, key, values) ? LOCKED_WHILE_ON[jobId][key].reason : undefined;
}
