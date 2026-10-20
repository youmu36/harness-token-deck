import fs from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import { AppError, asAppError, errorMessage, errorMessages } from '../src/app/errors';

describe('localized application errors', () => {
  it('covers defined backend codes, literal business errors and status failure codes', () => {
    const root = path.resolve(process.cwd(), 'backend/src/htu');
    const codes = new Set(['INTERNAL_ERROR', 'COLLECTION_FAILED', 'RETENTION_FAILED']);
    for (const name of fs.readdirSync(root, { recursive: true })) {
      if (typeof name !== 'string' || !name.endsWith('.py')) continue;
      const source = fs.readFileSync(path.join(root, name), 'utf8');
      for (const match of source.matchAll(/\b([A-Z][A-Z_]+):\s*Final\s*=\s*"([A-Z_]+)"/g)) codes.add(match[2]!);
      for (const match of source.matchAll(/DomainError\(\s*"([\w]+)"/g)) codes.add(match[1]!);
    }
    expect([...codes].filter(code => !errorMessages[code])).toEqual([]);
  });

  it('preserves backend messages for unknown codes, field metadata and log IDs', () => {
    expect(errorMessage({ code: 'FUTURE_ERROR', message: '后端新文案', retryable: false })).toBe('后端新文案');
    const error = new AppError({ code: 'WALLET_REVISION_CONFLICT', message: '冲突', retryable: false, field: 'expectedRevision', conflictRevision: 7 });
    expect(asAppError(error)).toBe(error);
    expect(error.info.field).toBe('expectedRevision');
    expect(error.info.conflictRevision).toBe(7);
    expect(errorMessage({ code: 'INTERNAL_ERROR', message: '内部错误', logId: 'log-123', retryable: false })).toContain('日志编号 log-123');
    expect(asAppError(Object.assign(new Error('契约漂移'), { code: 'CONTRACT_MISMATCH' })).code).toBe('CONTRACT_MISMATCH');
  });
});
