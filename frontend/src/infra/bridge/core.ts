import type { Bridge, CommandName, EventName, RequestMap, ResponseMap } from '../../app/ports';
import { AppError } from '../../app/errors';
import { createEventHub } from '../../shared/events';
import { commandValidators, eventValidators } from '../generated/lazyValidators';
import { ContractMismatch } from './contractError';
import type { Transport } from '../generated/commands';

type ValidatorsFor = (command: CommandName) => ReturnType<typeof commandValidators> | Awaited<ReturnType<typeof commandValidators>>;

export function createValidatedBridge(mode: Bridge['mode'], transport: Transport, hub = createEventHub(), onPending = (_count: number) => {}, validatorsFor: ValidatorsFor = commandValidators): Bridge {
  let pending = 0;
  return {
    mode,
    events: hub.source,
    async call<K extends CommandName>(command: K, payload: RequestMap[K]): Promise<ResponseMap[K]> {
      onPending(++pending);
      try {
        const selected = validatorsFor(command);
        const validators = 'then' in selected ? await selected : selected;
        if (!validators.request(payload)) {
          throw new AppError({ code: 'INVALID_REQUEST', message: '参数格式无效，请检查输入', retryable: false });
        }
        const response = await transport(command, payload);
        if (!validators.response(response)) throw new ContractMismatch(command);
        return response as ResponseMap[K];
      } finally { onPending(--pending); }
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
