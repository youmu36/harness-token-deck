export interface MigrationResult {
  migrationId: string;
  sourcePath: string;
  snapshotPath: string;
  reportPath: string;
  ok: boolean;
  sourceVersion?: string;
  oldTotal?: string;
  newTotal?: string;
  counts?: { table: string; old: number; new: number }[];
  vendors?: { name: string; old: string; new: string }[];
  warnings: string[];
  errors: string[];
}

export function migrationResult(value: unknown): MigrationResult | undefined {
  if (!value || typeof value !== 'object') return;
  const data = value as Record<string, unknown>;
  if (!['migrationId', 'sourcePath', 'snapshotPath', 'reportPath'].every(key => typeof data[key] === 'string')
    || typeof data.ok !== 'boolean'
    || !['warnings', 'errors'].every(key => Array.isArray(data[key]) && data[key].every(item => typeof item === 'string'))) return;
  if (data.ok) {
    const pico = (value: unknown) => typeof value === 'string' && /^(0|-?[1-9][0-9]*)$/.test(value);
    if (typeof data.sourceVersion !== 'string' || !pico(data.oldTotal) || !pico(data.newTotal)
      || !Array.isArray(data.counts) || !data.counts.every(row => row && typeof row.table === 'string'
        && Number.isSafeInteger(row.old) && row.old >= 0 && Number.isSafeInteger(row.new) && row.new >= 0)
      || !Array.isArray(data.vendors) || !data.vendors.every(row => row && typeof row.name === 'string' && pico(row.old) && pico(row.new))) return;
  }
  return data as unknown as MigrationResult;
}
