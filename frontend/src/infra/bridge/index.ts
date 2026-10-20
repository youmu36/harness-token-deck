import type { Bridge } from '../../app/ports';
import { createDesktopBridge, hasDesktopHost } from './desktop';

export type { Bridge, CommandName, CommandData, EventMap, RequestMap, ResponseMap } from '../../app/ports';
export { BridgeKey } from '../../app/ports';
export { unwrap } from '../../app/errors';

declare global {
  interface Window { __htu: { emit(name: string, payload: unknown): void } }
}

export async function createBridge(): Promise<Bridge> {
  if (hasDesktopHost()) return createDesktopBridge();
  const { createMockBridge } = await import('./mock');
  return createMockBridge();
}

export function installEvents(bridge: Bridge): () => void {
  const receiver = { emit: (name: string, payload: unknown) => {
    if (bridge.receiveEvent(name, payload)) window.dispatchEvent(new CustomEvent(name, { detail: payload }));
  } };
  window.__htu = receiver;
  let syncing = false;
  let disposed = false;
  const unsubscribe = bridge.events.onVisibility(visible => {
    if (!visible || syncing) return;
    syncing = true;
    void bridge.call('app.sync', {}).then(response => {
      if (response.ok && !disposed) {
        for (const event of response.data.pendingEvents) bridge.receiveEvent(event.name, event.payload);
      }
    }).catch(() => { /* Queries retain their own retryable errors; the next restoration retries sync. */ })
      .finally(() => { syncing = false; });
  });
  return () => {
    disposed = true;
    unsubscribe();
    if (window.__htu === receiver) window.__htu = { emit() {} };
  };
}
