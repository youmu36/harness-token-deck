import { inject, readonly, ref, shallowRef } from 'vue';
import { BridgeKey } from '../app/ports';
import type { Bridge, CommandData, CommandPayload, RequestMap, WriteCommand } from '../app/ports';
import { AppError, asAppError, unwrap } from '../app/errors';
import { canonicalPayload } from './payload';

// Only an unchanged failed request reuses its idempotency key.
export function useCommand<K extends WriteCommand>(name: K, options: {
  bridge?: Bridge;
  onSuccess?: (data: CommandData<K>) => void;
} = {}) {
  const bridge = options.bridge ?? inject(BridgeKey);
  if (!bridge) throw new Error('useCommand requires an application bridge');
  const pending = ref(false);
  const error = shallowRef<AppError>();
  const operationId = ref<string>();
  let failedPayload: string | undefined;

  async function run(payload: CommandPayload<K>): Promise<CommandData<K>> {
    if (pending.value) throw new AppError({ code: 'COMMAND_PENDING', message: '操作正在进行', retryable: false });
    const fingerprint = canonicalPayload(payload);
    if (failedPayload !== fingerprint || !operationId.value) operationId.value = crypto.randomUUID();
    // Detach the in-flight request from reactive editing drafts.
    const request = { ...JSON.parse(fingerprint), operationId: operationId.value } as RequestMap[K];
    pending.value = true;
    error.value = undefined;
    try {
      const result = unwrap<CommandData<K>>(await bridge!.call(name, request));
      failedPayload = typeof result === 'object' && result !== null && 'complete' in result && result.complete === false ? fingerprint : undefined;
      if (options.onSuccess) {
        try { options.onSuccess(result); } catch (failure) { console.error(failure); }
      }
      return result;
    } catch (failure) {
      failedPayload = fingerprint;
      error.value = asAppError(failure);
      throw error.value;
    } finally { pending.value = false; }
  }

  return { run, pending: readonly(pending), error: readonly(error), operationId: readonly(operationId) };
}
