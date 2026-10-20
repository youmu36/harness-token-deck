import type { EventMap, EventName, EventSource } from '../app/ports';

export function createEventHub(doc: Document = document, report: (error: unknown) => void = console.error) {
  const listeners = new Map<EventName, Set<(payload: never) => void>>();
  const visibilityListeners = new Set<(visible: boolean) => void>();
  let hostVisible = true;
  let visible = doc.visibilityState !== 'hidden';
  const notify = <T>(callbacks: Iterable<(value: T) => void>, value: T) => {
    for (const callback of [...callbacks]) {
      try { callback(value); } catch (error) { report(error); }
    }
  };
  function updateVisibility() {
    const next = hostVisible && doc.visibilityState !== 'hidden';
    if (visible === next) return;
    visible = next;
    notify(visibilityListeners, visible);
  }
  doc.addEventListener('visibilitychange', updateVisibility);
  const source: EventSource = {
    subscribe(name, listener) {
      const callbacks = listeners.get(name) ?? new Set();
      listeners.set(name, callbacks);
      callbacks.add(listener as (payload: never) => void);
      return () => { callbacks.delete(listener as (payload: never) => void); };
    },
    onVisibility(listener) {
      visibilityListeners.add(listener);
      return () => { visibilityListeners.delete(listener); };
    },
    isVisible: () => visible,
  };
  return {
    source,
    emit<K extends EventName>(name: K, payload: EventMap[K]) {
      if (name === 'app.visibility') {
        hostVisible = (payload as EventMap['app.visibility']).visible;
        updateVisibility();
      }
      notify(listeners.get(name) ?? [], payload as never);
    },
    dispose() {
      doc.removeEventListener('visibilitychange', updateVisibility);
      listeners.clear();
      visibilityListeners.clear();
    },
  };
}
