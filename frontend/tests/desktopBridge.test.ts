import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Bridge } from '../src/app/ports';
import { createDesktopBridge, hasDesktopHost } from '../src/infra/bridge/desktop';
import { createBridge } from '../src/infra/bridge';

const bridges: Bridge[] = [];
const desktop = (options: Parameters<typeof createDesktopBridge>[0] = {}) => {
  const bridge = createDesktopBridge(options); bridges.push(bridge); return bridge;
};
afterEach(() => {
  bridges.splice(0).forEach(bridge => bridge.dispose());
  delete window.pywebview;
  delete (window as Window & { chrome?: unknown }).chrome;
  vi.useRealTimers();
});

describe('desktop bridge', () => {
  it('signals startup settlement only after page loading, pending reads and paint, once', async () => {
    vi.useFakeTimers();
    const postMessage = vi.fn();
    Object.assign(window, { chrome: { webview: { postMessage } } });
    let finish!: (value: unknown) => void;
    window.pywebview = { api: { call: () => new Promise(resolve => { finish = resolve; }) } };
    const bridge = desktop();
    bridge.host!.setReady(true);
    await vi.advanceTimersByTimeAsync(2000);
    expect(postMessage).toHaveBeenCalledExactlyOnceWith('htd.ready');
    const request = bridge.call('preferences.get', {});
    await vi.waitFor(() => expect(finish).toBeTypeOf('function'));
    bridge.host!.setContentReady!();
    await vi.advanceTimersByTimeAsync(2000);
    expect(postMessage).not.toHaveBeenCalledWith('htd.startupSettled');
    finish({ ok: false, error: { code: 'SERVER_BUSY', message: '忙', retryable: true } });
    await request;
    await vi.advanceTimersByTimeAsync(1000);
    expect(postMessage.mock.calls.filter(([message]) => message === 'htd.startupSettled')).toHaveLength(1);
    bridge.host!.setContentReady!();
    await vi.advanceTimersByTimeAsync(2000);
    expect(postMessage.mock.calls.filter(([message]) => message === 'htd.startupSettled')).toHaveLength(1);
  });

  it('cancels a queued startup settlement on disposal', async () => {
    vi.useFakeTimers();
    const postMessage = vi.fn();
    Object.assign(window, { chrome: { webview: { postMessage } } });
    const bridge = desktop();
    bridge.host!.setReady(true);
    bridge.host!.setContentReady!();
    bridge.dispose();
    await vi.advanceTimersByTimeAsync(2000);
    expect(postMessage).not.toHaveBeenCalledWith('htd.startupSettled');
  });
  it('selects preview in a browser and waits for injected desktop APIs', async () => {
    expect(hasDesktopHost()).toBe(false);
    const preview = await createBridge(); bridges.push(preview);
    expect(preview.mode).toBe('preview');
    Object.assign(window, { chrome: { webview: {} } });
    const bridge = await createBridge(); bridges.push(bridge);
    expect(bridge.mode).toBe('desktop');
    const call = vi.fn(async () => ({ ok: false, error: { code: 'SERVER_BUSY', message: '忙', retryable: true } }));
    const request = bridge.call('preferences.get', {});
    window.pywebview = { api: { call } };
    window.dispatchEvent(new Event('pywebviewready'));
    expect(await request).toMatchObject({ ok: false, error: { code: 'SERVER_BUSY' } });
    expect(call).toHaveBeenCalledExactlyOnceWith('preferences.get', {});
  });

  it('fails readiness explicitly instead of falling back to synthetic data', async () => {
    vi.useFakeTimers();
    const request = desktop({ readyTimeoutMs: 100 }).call('preferences.get', {});
    const rejected = expect(request).rejects.toMatchObject({ code: 'BRIDGE_UNAVAILABLE' });
    await vi.dynamicImportSettled();
    await vi.advanceTimersByTimeAsync(100);
    await rejected;
  });

  it('validates responses and rejects invalid requests before invoking native code', async () => {
    const call = vi.fn(async () => ({}));
    window.pywebview = { api: { call } };
    const bridge = desktop();
    await expect(bridge.call('preferences.get', {})).rejects.toMatchObject({ code: 'CONTRACT_MISMATCH' });
    await expect(bridge.call('app.hello', { clientProtocol: 0 })).rejects.toMatchObject({ code: 'INVALID_REQUEST' });
    expect(call).toHaveBeenCalledTimes(1);
  });

  it('classifies read and write timeouts and leaves already-started writes observable by retry', async () => {
    vi.useFakeTimers();
    const call = vi.fn(() => new Promise<never>(() => {}));
    window.pywebview = { api: { call } };
    const bridge = desktop({ timeoutMs: 100 });
    const read = bridge.call('preferences.get', {});
    const write = bridge.call('preferences.update', {
      operationId: '00000000-0000-4000-8000-000000000001', expectedRevision: 0, patch: { theme: 'dark' },
    });
    const readFailure = expect(read).rejects.toMatchObject({ code: 'READ_TIMEOUT', info: { retryable: true } });
    const writeFailure = expect(write).rejects.toMatchObject({ code: 'WRITE_TIMEOUT', info: { retryable: true } });
    await vi.dynamicImportSettled();
    await vi.advanceTimersByTimeAsync(100);
    await Promise.all([readFailure, writeFailure]);
    expect(call).toHaveBeenCalledTimes(2);
    expect(vi.getTimerCount()).toBe(0);
  });
});
