import { describe, expect, it, vi } from 'vitest';
import { createValidatedBridge } from '../src/infra/bridge/core';
import { createMockBridge } from '../src/infra/bridge/mock';
import { useCommand } from '../src/shared/command';
import type { RequestMap } from '../src/app/ports';

const payload = { patch: { theme: 'dark' as const }, expectedRevision: 0 };

describe('useCommand', () => {
  it('reuses an operationId after a timeout even when the backend already committed', async () => {
    const backend = createMockBridge();
    const ids: string[] = [];
    let first = true;
    const client = createValidatedBridge('preview', async (_name, request) => {
      const update = request as RequestMap['preferences.update'];
      ids.push(update.operationId);
      const response = await backend.call('preferences.update', update);
      if (first) { first = false; return { ok: false, error: { code: 'WRITE_TIMEOUT', message: '超时', retryable: true } }; }
      return response;
    });
    const success = vi.fn();
    const command = useCommand('preferences.update', { bridge: client, onSuccess: success });
    await expect(command.run(payload)).rejects.toMatchObject({ code: 'WRITE_TIMEOUT' });
    expect(command.pending.value).toBe(false);
    expect(command.error.value?.code).toBe('WRITE_TIMEOUT');
    const result = await command.run({ expectedRevision: 0, patch: { theme: 'dark' } });
    expect(result.revision).toBe(1);
    expect(ids[0]).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    expect(ids[1]).toBe(ids[0]);
    expect(command.error.value).toBeUndefined();
    expect(success).toHaveBeenCalledTimes(1);
    await command.run({ patch: { theme: 'light' }, expectedRevision: 1 });
    expect(ids[2]).not.toBe(ids[1]);
    backend.dispose(); client.dispose();
  });

  it('allocates a new key for changed input and keeps failures local', async () => {
    const ids: string[] = [];
    const bridge = createValidatedBridge('preview', async (_name, request) => {
      ids.push((request as RequestMap['preferences.update']).operationId);
      return { ok: false, error: { code: 'PREFERENCES_REVISION_CONFLICT', message: '冲突', field: 'expectedRevision', conflictRevision: 3, retryable: false } };
    });
    const command = useCommand('preferences.update', { bridge });
    await expect(command.run(payload)).rejects.toMatchObject({ code: 'PREFERENCES_REVISION_CONFLICT' });
    await expect(command.run({ ...payload, expectedRevision: 3 })).rejects.toMatchObject({ code: 'PREFERENCES_REVISION_CONFLICT' });
    expect(ids[1]).not.toBe(ids[0]);
    expect(command.error.value?.info.field).toBe('expectedRevision');
    expect(command.error.value?.info.conflictRevision).toBe(3);
    bridge.dispose();
  });

  it('blocks duplicate clicks on itself and detaches requests from editable drafts', async () => {
    let complete!: (value: unknown) => void;
    let sent: RequestMap['preferences.update'] | undefined;
    const bridge = createValidatedBridge('preview', async (_name, request) => {
      sent = request as RequestMap['preferences.update'];
      return new Promise(resolve => { complete = resolve; });
    });
    const command = useCommand('preferences.update', { bridge });
    const draft = { patch: { theme: 'dark' as 'light' | 'dark' }, expectedRevision: 0 };
    const pending = command.run(draft);
    const id = command.operationId.value;
    draft.patch.theme = 'light';
    expect(sent?.patch.theme).toBe('dark');
    await expect(command.run(draft)).rejects.toMatchObject({ code: 'COMMAND_PENDING' });
    expect(command.operationId.value).toBe(id);
    expect(command.error.value).toBeUndefined();
    const other = createMockBridge();
    await useCommand('preferences.update', { bridge: other }).run(payload);
    complete({ ok: false, error: { code: 'WRITE_TIMEOUT', message: '超时', retryable: true } });
    await expect(pending).rejects.toMatchObject({ code: 'WRITE_TIMEOUT' });
    bridge.dispose(); other.dispose();
  });
});
