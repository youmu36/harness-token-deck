import { afterEach, describe, expect, it, vi } from 'vitest';
import { unwrap } from '../src/app/errors';
import type { Bridge, RequestMap } from '../src/app/ports';
import { createMockBridge } from '../src/infra/bridge/mock';
import { responseValidators } from '../src/infra/generated/commands';
import packageInfo from '../../package.json';

const bridges: Bridge[] = [];
const mock = () => { const bridge = createMockBridge(); bridges.push(bridge); return bridge; };
const id = (value: number) => `00000000-0000-4000-8000-${String(value).padStart(12, '0')}`;
const request: RequestMap['stats.summary'] = {
  harness: 'all', start: '2026-10-01T00:00:00.000Z', end: '2026-10-02T00:00:00.000Z', offsetMinutes: 480, grain: 'hour',
};
afterEach(() => bridges.splice(0).forEach(bridge => bridge.dispose()));

describe('browser preview bridge', () => {
  it('validates implemented successes, protocol errors and unsupported commands with generated Ajv', async () => {
    const bridge = mock();
    expect(bridge.mode).toBe('preview');
    const hello = unwrap(await bridge.call('app.hello', { clientProtocol: 1 }));
    expect(hello.protocol).toBe(1);
    expect(hello.capabilities).toContain('wallet.calibrate');
    expect(hello.dataDir).toBe('C:\\Preview\\HarnessTokenUsage');
    expect(hello.appVersion).toBe(packageInfo.version);
    const mismatch = await bridge.call('app.hello', { clientProtocol: 2 });
    expect(mismatch).toMatchObject({ ok: false, error: { code: 'PROTOCOL_MISMATCH' } });
    expect(responseValidators['app.hello'](mismatch)).toBe(true);
    expect(unwrap(await bridge.call('app.sync', {})).pendingEvents).toEqual([]);
    await expect(bridge.call('stats.summary', { ...request, offsetMinutes: 841 })).rejects.toMatchObject({ code: 'INVALID_REQUEST' });
  });

  it('keeps independent in-memory preferences and detaches snapshots from callers', async () => {
    const bridge = mock();
    const other = mock();
    const changes = vi.fn();
    bridge.events.subscribe('preferences.changed', changes);
    const original = unwrap(await bridge.call('preferences.get', {}));
    original.preferences.theme = 'dark';
    expect(unwrap(await bridge.call('preferences.get', {})).preferences.theme).toBe('system');
    const updated = unwrap(await bridge.call('preferences.update', { operationId: id(1), expectedRevision: 0, patch: { theme: 'dark' } }));
    expect(updated.preferences.theme).toBe('dark');
    expect(updated.revision).toBe(1);
    expect(changes).toHaveBeenCalledExactlyOnceWith({ revision: 1 });
    expect(unwrap(await other.call('preferences.get', {})).revision).toBe(0);
    updated.preferences.theme = 'light';
    expect(unwrap(await bridge.call('preferences.get', {})).preferences.theme).toBe('dark');
    await bridge.call('preferences.update', { operationId: id(2), expectedRevision: 1, patch: { theme: 'dark' } });
    expect(changes).toHaveBeenCalledTimes(1);
  });

  it('replays committed results before revision checks and rejects changed payloads using the same id', async () => {
    const bridge = mock();
    const write = { operationId: id(1), expectedRevision: 0, patch: { theme: 'dark' as const, collapsed: true } };
    const first = await bridge.call('preferences.update', write);
    const replay = await bridge.call('preferences.update', { ...write, patch: { collapsed: true, theme: 'dark' } });
    expect(replay).toEqual(first);
    expect(await bridge.call('preferences.update', { ...write, patch: { theme: 'light' } })).toMatchObject({ ok: false, error: { code: 'OPERATION_ID_REUSED' } });
    expect(await bridge.call('preferences.update', { ...write, operationId: id(2) })).toMatchObject({ ok: false, error: { code: 'PREFERENCES_REVISION_CONFLICT', conflictRevision: 1 } });
  });

  it('merges filter pages, replaces a single page, and enforces semantic preference validation', async () => {
    const bridge = mock();
    await bridge.call('preferences.update', { operationId: id(1), expectedRevision: 0, patch: { filters: { codex: { metric: 'cost' } } } });
    await bridge.call('preferences.update', { operationId: id(2), expectedRevision: 1, patch: { filters: { zcode: { metric: 'hitRate' } } } });
    let preferences = unwrap(await bridge.call('preferences.get', {})).preferences;
    expect(preferences.filters.codex?.metric).toBe('cost');
    expect(preferences.filters.zcode?.metric).toBe('hitRate');
    await bridge.call('preferences.update', { operationId: id(3), expectedRevision: 2, patch: { filters: { codex: { grain: 'day' } } } });
    preferences = unwrap(await bridge.call('preferences.get', {})).preferences;
    expect(preferences.filters.codex?.metric).toBe('tokens');
    expect(preferences.filters.codex?.grain).toBe('day');
    expect(await bridge.call('preferences.update', { operationId: id(4), expectedRevision: 3, patch: { theme: null } })).toMatchObject({ ok: false, error: { code: 'INVALID_REQUEST', field: 'patch.theme' } });
    expect(await bridge.call('preferences.update', { operationId: id(5), expectedRevision: 3, patch: { filters: { codex: { range: 'custom' } } } })).toMatchObject({ ok: false, error: { code: 'INVALID_REQUEST' } });
  });

  it('reconciles synthetic metrics across trends, models, hours and harnesses', async () => {
    const bridge = mock();
    const summary = unwrap(await bridge.call('stats.summary', request));
    expect(summary.grain).toBe('hour');
    expect(summary.trend).toHaveLength(24);
    expect(summary.trend[0]?.bucket).toBe('2026-10-01T08:00:00+08:00');
    expect(summary.harnesses.map(item => item.harness)).toEqual(['claude', 'codex', 'zcode', 'dsh']);
    for (const list of [summary.trend, summary.models, summary.harnesses]) {
      expect(list.reduce((sum, item) => sum + item.inputTokens, 0)).toBe(summary.metrics.inputTokens);
      expect(list.reduce((sum, item) => sum + BigInt(item.picoUsd), 0n).toString()).toBe(summary.metrics.picoUsd);
    }
    expect(summary.hourlyTotals.reduce((sum, item) => sum + item.metrics.recordCount, 0)).toBe(summary.metrics.recordCount);
    expect(summary.hourly.reduce((sum, item) => sum + BigInt(item.metrics.picoUsd), 0n).toString()).toBe(summary.metrics.picoUsd);
    expect(summary.metrics.totalTokens).toBe(summary.metrics.inputTokens + summary.metrics.outputTokens);
    expect(summary.metrics.cacheHitRate).toBe(summary.metrics.cacheReadTokens / summary.metrics.inputTokens);
    const single = unwrap(await bridge.call('stats.summary', { ...request, harness: 'codex' }));
    const codex = summary.harnesses.find(item => item.harness === 'codex')!;
    expect(single.metrics).toMatchObject({
      recordCount: codex.recordCount,
      inputTokens: codex.inputTokens,
      outputTokens: codex.outputTokens,
      cacheReadTokens: codex.cacheReadTokens,
      picoUsd: codex.picoUsd,
    });
    expect(single.harnesses).toEqual([]);
    expect(await bridge.call('stats.summary', request)).toEqual({ ok: true, data: summary });
  });

  it('selects grain, handles half-open empty ranges and returns validated range errors', async () => {
    const bridge = mock();
    expect(unwrap(await bridge.call('stats.summary', { ...request, grain: null })).grain).toBe('minute');
    expect(unwrap(await bridge.call('stats.summary', { ...request, grain: 'minute', end: '2026-10-04T00:00:00.000Z' })).grain).toBe('hour');
    expect(unwrap(await bridge.call('stats.summary', { ...request, grain: 'hour', end: '2026-10-31T00:00:00.000Z' })).grain).toBe('hour');
    const empty = unwrap(await bridge.call('stats.summary', { ...request, start: '2026-10-01T00:01:00.000Z', end: '2026-10-01T00:02:00.000Z' }));
    expect(empty.metrics.recordCount).toBe(0);
    expect(empty.metrics.cacheHitRate).toBeNull();
    expect(empty.trend).toEqual([]);
    expect(await bridge.call('stats.summary', { ...request, end: request.start })).toMatchObject({ ok: false, error: { code: 'RANGE_INVALID' } });
    expect(await bridge.call('stats.summary', { ...request, end: '2026-11-01T00:00:00.000Z' })).toMatchObject({ ok: false, error: { code: 'RANGE_TOO_LONG' } });
    expect(await bridge.call('stats.summary', { ...request, start: '2026-02-30T00:00:00.000Z' })).toMatchObject({ ok: false, error: { code: 'INVALID_REQUEST' } });
  });
});
