import type { Bridge, CommandName, EventName, RequestMap, ResponseMap } from '../../app/ports';
import { AppError } from '../../app/errors';
import { createEventHub } from '../../shared/events';
import { call, eventValidators, requestValidators } from '../generated/commands';
import type { Transport } from '../generated/commands';

export function createValidatedBridge(mode: Bridge['mode'], transport: Transport, hub = createEventHub()): Bridge {
  return {
    mode,
    events: hub.source,
    async call<K extends CommandName>(command: K, payload: RequestMap[K]): Promise<ResponseMap[K]> {
      if (!requestValidators[command](payload)) {
        throw new AppError({ code: 'INVALID_REQUEST', message: '参数格式无效，请检查输入', retryable: false });
      }
      return call(transport, command, payload);
    },
    receiveEvent(name, payload) {
      if (!Object.hasOwn(eventValidators, name)) return false;
      const event = name as EventName;
      if (!eventValidators[event](payload)) return false;
      hub.emit(event, payload as never);
      return true;
    },
    dispose: () => hub.dispose(),
  };
}
