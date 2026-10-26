import { AppError } from '../../app/errors';
import type { Bridge } from '../../app/ports';
import { createValidatedBridge } from './core';

interface DesktopApi { call(command: string, payload: object): Promise<unknown> }
interface WebViewHost { postMessage(message: string): void }
const host = () => (window as Window & { chrome?: { webview?: WebViewHost } }).chrome?.webview;
declare global {
  interface Window { pywebview?: { api?: DesktopApi } }
}

export function hasDesktopHost(): boolean {
  // WebView2 exists before pywebview injects its API; never fall back to demo data while it starts.
  return !!window.pywebview || !!(window as Window & { chrome?: { webview?: unknown } }).chrome?.webview;
}

function ready(timeoutMs: number): Promise<DesktopApi> {
  const api = window.pywebview?.api;
  if (api) return Promise.resolve(api);
  return new Promise((resolve, reject) => {
    const cleanup = () => { clearTimeout(timer); window.removeEventListener('pywebviewready', onReady); };
    const onReady = () => {
      const current = window.pywebview?.api;
      if (!current) return;
      cleanup();
      resolve(current);
    };
    const timer = setTimeout(() => {
      cleanup();
      reject(new AppError({ code: 'BRIDGE_UNAVAILABLE', message: '本机应用连接超时', retryable: true }));
    }, timeoutMs);
    window.addEventListener('pywebviewready', onReady);
    onReady();
  });
}

export function createDesktopBridge(options: { timeoutMs?: number; readyTimeoutMs?: number } = {}): Bridge {
  let pending = 0, frontendReady = false, contentReady = false, settled = false, disposed = false;
  let idle: ReturnType<typeof setTimeout> | undefined;
  let frame: number | undefined;
  function cancelSettled() {
    clearTimeout(idle); idle = undefined;
    if (frame !== undefined) cancelAnimationFrame(frame);
    frame = undefined;
  }
  function scheduleSettled() {
    cancelSettled();
    if (disposed || settled || !frontendReady || !contentReady || pending) return;
    idle = setTimeout(() => {
      idle = undefined;
      frame = requestAnimationFrame(() => {
        frame = requestAnimationFrame(() => {
          frame = undefined;
          if (disposed || pending || !frontendReady) return;
          settled = true;
          host()?.postMessage('htd.startupSettled');
        });
      });
    }, 500);
  }
  const bridge = createValidatedBridge('desktop', async (command, payload) => {
    const api = await ready(options.readyTimeoutMs ?? 15_000);
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      return await Promise.race([
        api.call(command, payload),
        new Promise<never>((_, reject) => {
          timer = setTimeout(() => reject(new AppError({
            code: 'operationId' in payload ? 'WRITE_TIMEOUT' : 'READ_TIMEOUT',
            message: '等待本机应用响应超时', retryable: true,
          })), command === 'app.pickPath' ? 600_000 : options.timeoutMs ?? 35_000);
        }),
      ]);
    } catch (error) {
      if (error instanceof AppError) throw error;
      throw new AppError({ code: 'BRIDGE_UNAVAILABLE', message: '本机应用连接中断', retryable: true });
    } finally { clearTimeout(timer); }
  }, undefined, count => { pending = count; scheduleSettled(); });
  return { ...bridge, dispose: () => { disposed = true; cancelSettled(); bridge.dispose(); }, host: host() ? {
    openLogDir: () => host()?.postMessage('htd.openLogDir'),
    quit: () => host()?.postMessage('htd.quit'),
    setReady: (ready: boolean) => {
      frontendReady = ready;
      if (!ready) contentReady = false;
      host()?.postMessage(ready ? 'htd.ready' : 'htd.unready');
      scheduleSettled();
    },
    setContentReady: () => { contentReady = true; scheduleSettled(); },
  } : undefined };
}
