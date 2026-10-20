import type { Bridge, CommandName, RequestMap, ResponseMap } from './ports';
import { AppError } from './errors';
export const CLIENT_PROTOCOL = 1;

/** The injected application bridge cannot write before a successful startup. */
export function createStartupBridge(transport: Bridge) {
  let writable = false;
  const bridge: Bridge = {
    mode: transport.mode, events: transport.events, host: transport.host,
    receiveEvent: (name, payload) => transport.receiveEvent(name, payload),
    dispose: () => transport.dispose(),
    async call<K extends CommandName>(name: K, payload: RequestMap[K]): Promise<ResponseMap[K]> {
      if ('operationId' in payload && !writable) throw new AppError({
        code: 'PROTOCOL_MISMATCH', message: '握手尚未完成，不能保存更改', retryable: false,
      });
      return transport.call(name, payload);
    },
  };
  return { bridge, setWritable: (value: boolean) => { writable = value; } };
}
