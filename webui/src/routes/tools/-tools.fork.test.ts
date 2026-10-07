import { describe, expect, it } from 'vitest';

import { isRepairSettingLocked, repairSettingLockReason } from './-tools.fork';

/** fork (itsdmd/SoulSync): tag splitting and a custom separator are mutually exclusive. */
describe('comma artist splitter: separator lock', () => {
  it('locks the separator while tag splitting is on', () => {
    const on = { split_into_separate_tags: true, separator: 'semicolon' };
    expect(isRepairSettingLocked('comma_artist_splitter', 'separator', on)).toBe(true);
    expect(repairSettingLockReason('comma_artist_splitter', 'separator', on)).toMatch(/own tag/);
    expect(
      isRepairSettingLocked('comma_artist_splitter', 'separator', {
        split_into_separate_tags: 'true',
      }),
    ).toBe(true);
  });

  it('frees it when tag splitting is off', () => {
    const off = { split_into_separate_tags: false, separator: 'comma' };
    expect(isRepairSettingLocked('comma_artist_splitter', 'separator', off)).toBe(false);
    expect(repairSettingLockReason('comma_artist_splitter', 'separator', off)).toBeUndefined();
  });

  it('never locks other settings or other jobs', () => {
    const on = { split_into_separate_tags: true };
    expect(isRepairSettingLocked('comma_artist_splitter', 'dry_run', on)).toBe(false);
    expect(isRepairSettingLocked('comma_artist_splitter', 'split_into_separate_tags', on)).toBe(
      false,
    );
    expect(isRepairSettingLocked('library_retag', 'separator', on)).toBe(false);
  });

  it('opens the custom separator only when splitting is off and Separator is Custom', () => {
    const job = 'comma_artist_splitter';
    expect(
      isRepairSettingLocked(job, 'custom_separator', {
        split_into_separate_tags: true,
        separator: 'custom',
      }),
    ).toBe(true);
    expect(
      isRepairSettingLocked(job, 'custom_separator', {
        split_into_separate_tags: false,
        separator: 'comma',
      }),
    ).toBe(true);
    expect(
      repairSettingLockReason(job, 'custom_separator', {
        split_into_separate_tags: false,
        separator: 'comma',
      }),
    ).toMatch(/Custom/);
    expect(
      isRepairSettingLocked(job, 'custom_separator', {
        split_into_separate_tags: false,
        separator: 'custom',
      }),
    ).toBe(false);
    // what to detect is independent of how the result is written
    expect(isRepairSettingLocked(job, 'extra_splitters', { split_into_separate_tags: true })).toBe(
      false,
    );
  });
});
