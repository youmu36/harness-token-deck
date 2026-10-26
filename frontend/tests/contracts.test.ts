import { describe, expect, it } from 'vitest';
import recordedData from '../../fixtures/ipc/responses.json';
import {
  call, ContractMismatch, eventValidators, requestValidators, responseValidators, validateResponse,
} from '../src/infra/generated/commands';
import type { CommandName, ResponseMap } from '../src/infra/generated/commands';
import { commandValidators } from '../src/infra/generated/lazyValidators';

const recordings = recordedData as { command: CommandName; payload: object; response: unknown }[];

describe('generated IPC contracts', () => {
  it('loads the same strict Ajv validators through the lazy command path', async () => {
    for (const recording of recordings) {
      const lazy = await commandValidators(recording.command);
      expect(lazy.request).toBe(requestValidators[recording.command]);
      expect(lazy.response).toBe(responseValidators[recording.command]);
      expect(lazy.response(recording.response)).toBe(true);
      expect(lazy.response({ ok: true, data: null })).toBe(false);
      expect(lazy.request({ ...recording.payload, __unexpected: true })).toBe(false);
    }
  });
  it('validates genuine router envelopes for every registered command', () => {
    const covered = new Set<string>();
    const coveredEvents = new Set<string>();
    for (const recording of recordings) {
      const command = recording.command as CommandName;
      covered.add(command);
      const validator = responseValidators[command];
      expect(validator(recording.response), JSON.stringify(validator.errors)).toBe(true);
      const response = validateResponse(command, recording.response);
      if (response.ok) expect(requestValidators[command](recording.payload)).toBe(true);
      expect(validateResponse(command, recording.response)).toBe(recording.response);
      if (command === 'app.sync') {
        const sync = validateResponse('app.sync', recording.response);
        if (!sync.ok) throw new Error(sync.error.message);
        for (const event of sync.data.pendingEvents) {
          coveredEvents.add(event.name);
          expect(eventValidators[event.name](event.payload)).toBe(true);
        }
      }
    }
    expect([...covered].sort()).toEqual(Object.keys(responseValidators).sort());
    expect([...coveredEvents].sort()).toEqual(Object.keys(eventValidators).sort());
    const failures = recordings.map(record => validateResponse(record.command, record.response)).filter(response => !response.ok);
    expect(failures.some(response => response.error.code === 'INVALID_REQUEST')).toBe(true);
    expect(failures.some(response => response.error.code === 'PREFERENCES_REVISION_CONFLICT')).toBe(true);
  });

  it('rejects malformed envelopes, omitted defaults, unsafe counts and numeric money', () => {
    const snapshot = validateResponse('preferences.get', recordings.find(record => record.command === 'preferences.get')!.response);
    if (!snapshot.ok) throw new Error(snapshot.error.message);
    const missingOk = structuredClone(snapshot) as unknown as Record<string, unknown>;
    delete missingOk.ok;
    expect(() => validateResponse('preferences.get', missingOk)).toThrow(ContractMismatch);
    expect(responseValidators['preferences.get']({ ...snapshot, extra: true })).toBe(false);
    expect(responseValidators['preferences.get']({ ok: true, data: { preferences: {}, revision: 0 } })).toBe(false);
    expect(responseValidators['preferences.get']({ ok: true, data: { ...snapshot.data, revision: 2 ** 53 } })).toBe(false);
    const wallet = structuredClone(validateResponse('wallet.list', recordings.find(record => record.command === 'wallet.list')!.response));
    if (wallet.ok) {
      (wallet.data.vendors[0] as unknown as Record<string, unknown>).B = 8750000000000;
    }
    expect(responseValidators['wallet.list'](wallet)).toBe(false);
    expect(responseValidators['wallet.list']({ ok: false, error: { code: 'ERROR', message: '失败' } })).toBe(false);
    expect(eventValidators['usage.changed']({ harnesses: ['codex'], revision: -1 })).toBe(false);
  });

  it('preserves exact pico strings through the typed wrapper and rejects contract drift', async () => {
    const recorded = recordings.find(record => record.command === 'wallet.list')!.response;
    const response = await call(async () => recorded, 'wallet.list', {});
    if (!response.ok) throw new Error(response.error.message);
    const typed: ResponseMap['wallet.list'] = response;
    expect(typed.data.vendors[0]!.R).toBe('10000123456000000');
    await expect(call(async () => ({}), 'wallet.list', {})).rejects.toMatchObject({ code: 'CONTRACT_MISMATCH' });
    const failure = await call(async () => ({ ok: false, error: { code: 'SERVER_BUSY', message: '稍后重试', retryable: true } }), 'wallet.list', {});
    expect(failure.ok).toBe(false);
  });
});
