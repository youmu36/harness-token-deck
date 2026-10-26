import { getCurrentScope, onScopeDispose, reactive, ref, shallowRef } from 'vue';
import type { Bridge, CommandData, RequestMap } from '../../app/ports';
import { asAppError, unwrap } from '../../app/errors';
import { walletAmountInput } from '../../shared/money';

export type AmountDraft = { text: string; originalText: string; originalPico?: string };
export const amountDraft = (pico?: string): AmountDraft => {
  const text = pico === undefined ? '' : walletAmountInput(pico);
  return { text, originalText: text, originalPico: pico };
};
export function amountPico(draft: AmountDraft): string {
  if (draft.originalPico !== undefined && draft.text === draft.originalText) return draft.originalPico;
  if (!/^\d{1,26}(?:\.\d{1,6})?$/.test(draft.text)) throw new RangeError('金额须为非负数，最多六位小数');
  const [whole, part = ''] = draft.text.split('.');
  return (BigInt(whole!) * 10n ** 12n + BigInt(part.padEnd(12, '0'))).toString();
}
export type AllocationDraft = { day: string; model: string; harness: string; group: string; version: string; session: string; confirm: boolean; amount: AmountDraft;
  original?: CommandData<'reconciliation.targets'>['targets'][number]['allocations'][number] };
type Commit = RequestMap['reconciliation.commitCalibration'];
type SessionEntry = {session:ReturnType<typeof createSession>;readers:number};
const sessions = new WeakMap<Bridge, Map<string, SessionEntry>>();

function createSession(bridge: Bridge, vendorId: string, onSettled:()=>void) {
  const now = new Date();
  const draft = reactive({ start: new Date(now.getTime() - 3 * 86400000).toISOString(), end: now.toISOString(),
    batchIds: [] as string[], intervalIds: [] as string[], target: amountDraft(), note: '', basis: 'manual' as 'manual' | 'remote',
    confirmScope: false, acknowledgeIncomplete: false, confirmReplacement: false, replaceIds: [] as string[],
    days: [] as { day: string; amount: AmountDraft }[], allocations: [] as AllocationDraft[],
    allocationMode: 'proportional' as 'explicit' | 'unallocated' | 'proportional' | 'weighted', modelWeights: {} as Record<string, string>,
    action: 'apply' as 'apply' | 'revoke', targetId: undefined as string | undefined });
  const pending = ref(false), error = ref(''), message = ref('');
  const preview = shallowRef<CommandData<'reconciliation.previewCalibration'>>();
  const plan = shallowRef<RequestMap['reconciliation.previewCalibration']>();
  const storageKey = `htd.calibration.pending.${bridge.mode}.${vendorId}`;
  const uncertain = shallowRef<Commit>();
  let stored = false;
  try { const saved = localStorage.getItem(storageKey); if (saved) {uncertain.value = JSON.parse(saved) as Commit;stored=true;} } catch { /* Storage may be disabled. */ }
  async function submit(request: Commit) {
    if (pending.value) return;
    pending.value = true; error.value = ''; message.value = '';
    uncertain.value = request;
    stored=false;
    try { localStorage.setItem(storageKey, JSON.stringify(request)); stored=true; } catch { /* The server still keeps receipts. */ }
    try {
      const response = unwrap(await bridge.call('reconciliation.commitCalibration', request));
      message.value = response.action === 'revoke' ? '已撤销目标，按当前估算恢复本地消费。' : '已锁定目标，本地消费和余额已更新。';
      uncertain.value = undefined; preview.value = undefined; plan.value = undefined;
      draft.targetId = undefined; draft.replaceIds = []; draft.action = 'apply';
      try { localStorage.removeItem(storageKey); } catch { /* Storage may be disabled. */ }
      return response;
    } catch (failure) {
      const appError = asAppError(failure); error.value = appError.message;
      // A domain rejection confirms no write; transport/timeouts retain the exact authorized request.
      if (!appError.info.retryable && !['WRITE_TIMEOUT', 'BRIDGE_TIMEOUT', 'BRIDGE_UNAVAILABLE'].includes(appError.code)) {
        uncertain.value = undefined;
        try { localStorage.removeItem(storageKey); } catch { /* Storage may be disabled. */ }
        preview.value = undefined; plan.value = undefined;
      }
    } finally { pending.value = false; onSettled(); }
  }
  async function commit() {
    if (uncertain.value) return submit(uncertain.value);
    if (!preview.value || !plan.value) return;
    return submit({ ...plan.value, fingerprint: preview.value.fingerprint, operationId: crypto.randomUUID() });
  }
  return { draft, pending, error, message, preview, plan, uncertain, commit,
    canRelease:()=>!pending.value && (!uncertain.value || stored) };
}
export function calibrationSession(bridge: Bridge, vendor: string) {
  let byVendor = sessions.get(bridge);
  if (!byVendor) { byVendor = new Map(); sessions.set(bridge, byVendor); }
  let entry = byVendor.get(vendor);
  if (!entry) entry = {session:createSession(bridge,vendor,trim),readers:0};
  byVendor.delete(vendor);byVendor.set(vendor,entry);
  const current=entry, values=byVendor;
  function trim() {
    // Active editors and an unpersisted uncertain write retain their owners.
    const inactive=[...values].filter(([,value])=>!value.readers && value.session.canRelease());
    for(const [key] of inactive.slice(0,Math.max(0,inactive.length-8)))values.delete(key);
  }
  if(getCurrentScope()) {
    current.readers++;
    onScopeDispose(()=>{current.readers--;trim();});
  }
  trim();
  return current.session;
}
