import { afterEach, expect, it, vi } from 'vitest';
import { createMockBridge } from '../src/infra/bridge/mock';
import { readShared } from '../src/shared/readService';

const cleanup: (() => void)[] = [];
afterEach(() => { cleanup.splice(0).forEach(stop => stop()); vi.restoreAllMocks(); });
it('shares normalized reads and keeps the transport alive for the remaining reader', async () => {
  const bridge = createMockBridge(); cleanup.push(() => bridge.dispose());
  const original = bridge.call.bind(bridge); let release!: () => void;
  const wait = new Promise<void>(resolve => { release = resolve; });
  const call = vi.spyOn(bridge, 'call').mockImplementation(async (name, payload) => {
    if (name === 'stats.explore') await wait;
    return original(name, payload);
  });
  const one = new AbortController(), two = new AbortController();
  const parameters = { start: '2026-10-01T00:00:00.000Z', end: '2026-10-02T00:00:00.000Z', modelKeys: ['gpt-6', 'deepseek-r1'] };
  const first = readShared(bridge, 'stats.explore', parameters, one.signal);
  const second = readShared(bridge, 'stats.explore', { ...parameters, modelKeys: [...parameters.modelKeys].reverse() }, two.signal);
  const cancelled = expect(first).rejects.toMatchObject({ code: 'READ_CANCELLED' }); one.abort(); await cancelled;
  expect(call.mock.calls.filter(([name]) => name === 'stats.explore')).toHaveLength(1);
  expect(call.mock.calls.some(([name]) => name === 'stats.cancelAnalysis')).toBe(false);
  release(); expect((await second).models.length).toBeGreaterThan(0);
  expect(call.mock.calls.some(([name]) => name === 'stats.cancelAnalysis')).toBe(false);
});

it('cancels only when the final reader departs and a subsequent request gets a fresh identity', async () => {
  const bridge = createMockBridge(); cleanup.push(() => bridge.dispose()); const original = bridge.call.bind(bridge);
  let release!: () => void; const wait = new Promise<void>(resolve => { release = resolve; });
  const call = vi.spyOn(bridge, 'call').mockImplementation(async (name, payload) => { if (name === 'stats.explore') await wait; return original(name, payload); });
  const parameters = { start: '2026-10-01T00:00:00.000Z', end: '2026-10-02T00:00:00.000Z' };
  const one = new AbortController(), two = new AbortController();
  const first = readShared(bridge, 'stats.explore', parameters, one.signal), second = readShared(bridge, 'stats.explore', parameters, two.signal);
  const rejected = Promise.all([expect(first).rejects.toMatchObject({ code: 'READ_CANCELLED' }), expect(second).rejects.toMatchObject({ code: 'READ_CANCELLED' })]);
  one.abort(); two.abort(); await rejected;
  expect(call.mock.calls.filter(([name]) => name === 'stats.cancelAnalysis')).toHaveLength(1);
  const next = readShared(bridge, 'stats.explore', parameters, new AbortController().signal); release(); await next;
  const ids = call.mock.calls.filter(([name]) => name === 'stats.explore').map(([, payload]) => (payload as { queryId: string }).queryId);
  expect(new Set(ids).size).toBe(2);
});
