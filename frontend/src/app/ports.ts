import type { InjectionKey } from 'vue';
import type { CommandName, EventMap, EventName, RequestMap, ResponseMap } from '../infra/generated/commands';

// Application ports keep shared composables independent of transport implementations.
export type { CommandName, EventMap, EventName, RequestMap, ResponseMap };
export type CommandData<K extends CommandName> = Extract<ResponseMap[K], { ok: true }>['data'];
export type WriteCommand = { [K in CommandName]: RequestMap[K] extends { operationId: string } ? K : never }[CommandName];
export type CommandPayload<K extends WriteCommand> = Omit<RequestMap[K], 'operationId'>;

export interface EventSource {
  subscribe<K extends EventName>(name: K, listener: (payload: EventMap[K]) => void): () => void;
  onVisibility(listener: (visible: boolean) => void): () => void;
  isVisible(): boolean;
}

export interface Bridge {
  readonly mode: 'desktop' | 'preview';
  readonly events: EventSource;
  readonly host?: { openLogDir(): void; quit(): void; setReady(ready: boolean): void };
  call<K extends CommandName>(command: K, payload: RequestMap[K]): Promise<ResponseMap[K]>;
  receiveEvent(name: string, payload: unknown): boolean;
  dispose(): void;
}

export const BridgeKey: InjectionKey<Bridge> = Symbol('htu.bridge');
export const NavigationGuardKey: InjectionKey<(guard: (proceed: () => void) => void) => (() => void)> = Symbol('htu.navigationGuard');
