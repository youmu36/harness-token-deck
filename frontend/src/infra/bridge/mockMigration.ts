import { AppError } from '../../app/errors';
import type { CommandData, RequestMap } from '../../app/ports';
import { canonicalPayload } from '../../shared/payload';
import type { createMockTasks } from './mockTasks';

export interface MockMigrationOptions { detected?: boolean; legacyRunning?: boolean; targetEmpty?: boolean; fail?: boolean }
export function createMockMigration(tasks: ReturnType<typeof createMockTasks>, options: MockMigrationOptions, changed: () => void) {
  let activated = false;
  const results = new Map<string, string>();
  const runs = new Map<string, CommandData<'migration.run'>>();
  const receipts = new Map<string, { fingerprint: string; result: CommandData<'migration.activate'> }>();
  const sourcePath = 'C:\\Users\\Example\\AppData\\Roaming\\com.harness-token-usage.desktop\\ledger.sqlite3';
  const directory = (id: string) => `C:\\HTU-Preview\\migration\\${id}`;
  const fail = (code: string, message: string): never => { throw new AppError({ code, message, retryable: false }); };
  const detect = (selectedPath?: string | null): CommandData<'migration.detect'> => {
    const detected = options.detected ?? true; const legacyRunning = options.legacyRunning ?? false;
    const targetEmpty = (options.targetEmpty ?? true) && !activated;
    return { detected, legacyRunning, targetEmpty, sourcePath: selectedPath ?? sourcePath, canMigrate: detected && !legacyRunning && targetEmpty,
      reason: !detected ? '未发现旧版数据库' : legacyRunning ? '请先退出旧版 Harness Token Usage 再迁移' : !targetEmpty ? '新版已有账本或钱包凭证，不能自动导入' : null };
  };
  function run(request: RequestMap['migration.run']): CommandData<'migration.run'> {
    const selectedPath = request.sourcePath ?? sourcePath;
    const previous = runs.get(request.operationId); if (previous) return structuredClone(previous);
    const id = crypto.randomUUID(); const reportPath = `${directory(id)}\\report.md`;
    const started = tasks.start('migration.run', request, () => {
      const base = { migrationId: id, sourcePath: selectedPath, snapshotPath: `${directory(id)}\\legacy-snapshot.sqlite3`, reportPath };
      if (options.fail || !detect().canMigrate) return { ...base, ok: false, errors: [detect().reason ?? '迁移校验失败'], warnings: [] };
      results.set(id, selectedPath);
      return { ...base, ok: true, sourceVersion: '0.2.3~0.3.0', oldTotal: '137435606239200', newTotal: '137435606239200',
        counts: [{ table: 'ledger', old: 6005, new: 6005 }, { table: 'identity', old: 6005, new: 6005 }, { table: 'checkpoint', old: 235, new: 235 }, { table: 'recalc_batch', old: 16, new: 16 }],
        vendors: [{ name: 'North API', old: '42000000000000', new: '42000000000000' }, { name: 'Green Compute', old: '18500000000000', new: '18500000000000' }],
        warnings: ['供应商关联时间线按迁移时关联推定'], errors: [] };
    });
    const result = { ...started, migrationId: id, reportPath }; runs.set(request.operationId, result); return structuredClone(result);
  }
  function activate(request: RequestMap['migration.activate']): CommandData<'migration.activate'> {
    const fingerprint = canonicalPayload(request); const receipt = receipts.get(request.operationId);
    if (receipt) { if (receipt.fingerprint !== fingerprint) fail('OPERATION_ID_REUSED', '操作标识已用于其他参数'); return structuredClone(receipt.result); }
    const detection = detect();
    if (detection.legacyRunning) fail('LEGACY_RUNNING', detection.reason!);
    if (!detection.targetEmpty) fail('MIGRATION_TARGET_NOT_EMPTY', detection.reason!);
    if (!results.has(request.migrationId)) fail('MIGRATION_VERIFICATION_FAILED', '迁移校验未通过，不能激活');
    const result = { activated: true, sourcePath: results.get(request.migrationId)!, targetPath: 'C:\\HTU-Preview\\htu.sqlite3', reportPath: `${directory(request.migrationId)}\\report.md` };
    receipts.set(request.operationId, { fingerprint, result }); activated = true; changed(); return structuredClone(result);
  }
  return { detect, run, activate };
}
