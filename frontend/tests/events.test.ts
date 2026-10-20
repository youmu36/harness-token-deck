import { describe, expect, it, vi } from 'vitest';
import { flushPromises } from '@vue/test-utils';
import { createEventHub } from '../src/shared/events';
import { createValidatedBridge } from '../src/infra/bridge/core';
import { installEvents } from '../src/infra/bridge';

describe('event subscriptions', () => {
  it('combines host and document visibility and isolates subscriber failures', () => {
    const doc = new Document();
    Object.defineProperty(doc, 'visibilityState', { value: 'visible', configurable: true });
    const report = vi.fn();
    const hub = createEventHub(doc, report);
    const visibility = vi.fn();
    const usage = vi.fn();
    hub.source.onVisibility(visibility);
    hub.source.subscribe('usage.changed', () => { throw new Error('subscriber'); });
    const unsubscribe = hub.source.subscribe('usage.changed', usage);
    hub.emit('usage.changed', { harnesses: ['codex'], revision: 1 });
    expect(usage).toHaveBeenCalledTimes(1);
    expect(report).toHaveBeenCalledTimes(1);
    unsubscribe();
    hub.emit('app.visibility', { visible: false });
    Object.defineProperty(doc, 'visibilityState', { value: 'hidden', configurable: true });
    doc.dispatchEvent(new Event('visibilitychange'));
    hub.emit('app.visibility', { visible: true });
    expect(hub.source.isVisible()).toBe(false);
    Object.defineProperty(doc, 'visibilityState', { value: 'visible', configurable: true });
    doc.dispatchEvent(new Event('visibilitychange'));
    expect(visibility.mock.calls).toEqual([[false], [true]]);
    hub.dispose();
    hub.emit('usage.changed', { harnesses: ['codex'], revision: 2 });
    expect(usage).toHaveBeenCalledTimes(1);
  });

  it('validates native events and syncs buffered events on restoration', async () => {
    const transport = vi.fn(async () => ({ ok: true, data: {
      revisions: { usage: 2 }, pendingEvents: [{ name: 'usage.changed', payload: { harnesses: ['codex'], revision: 2 } }],
    } }));
    const bridge = createValidatedBridge('desktop', transport);
    const usage = vi.fn();
    const nativeVisibility = vi.fn();
    window.addEventListener('app.visibility', nativeVisibility);
    bridge.events.subscribe('usage.changed', usage);
    const dispose = installEvents(bridge);
    expect(bridge.receiveEvent('unknown', {})).toBe(false);
    expect(bridge.receiveEvent('usage.changed', { harnesses: ['codex'], revision: -1 })).toBe(false);
    window.__htu.emit('app.visibility', { visible: false });
    expect(transport).not.toHaveBeenCalled();
    window.__htu.emit('app.visibility', { visible: true });
    await flushPromises();
    expect(transport).toHaveBeenCalledExactlyOnceWith('app.sync', {});
    expect(usage).toHaveBeenCalledExactlyOnceWith({ harnesses: ['codex'], revision: 2 });
    expect(nativeVisibility).toHaveBeenCalledTimes(2);
    expect((nativeVisibility.mock.calls[1]![0] as CustomEvent).detail).toEqual({ visible: true });
    dispose();
    window.__htu.emit('usage.changed', { harnesses: ['codex'], revision: 3 });
    expect(usage).toHaveBeenCalledTimes(1);
    window.removeEventListener('app.visibility', nativeVisibility);
    bridge.dispose();
  });

  it('ignores a late synchronization result after uninstalling the receiver', async () => {
    let resolve!: (value: unknown) => void;
    const bridge = createValidatedBridge('desktop', () => new Promise(yes => { resolve = yes; }));
    const usage = vi.fn();
    bridge.events.subscribe('usage.changed', usage);
    const uninstall = installEvents(bridge);
    window.__htu.emit('app.visibility', { visible: false });
    window.__htu.emit('app.visibility', { visible: true });
    uninstall();
    resolve({ ok: true, data: { revisions: { usage: 1 }, pendingEvents: [
      { name: 'usage.changed', payload: { harnesses: ['codex'], revision: 1 } },
    ] } });
    await flushPromises();
    expect(usage).not.toHaveBeenCalled();
    bridge.dispose();
  });
});
