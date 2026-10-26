import { AppError } from '../../app/errors';
import type { CommandData, EventMap, EventName, RequestMap } from '../../app/ports';
import { canonicalPayload } from '../../shared/payload';
import { reconciliationPreview } from './mockReconciliation';

type Parameters = RequestMap['reconciliation.previewCalibration'];
type Target = CommandData<'reconciliation.targets'>['targets'][number];
const fail = (code: string): never => { throw new AppError({ code, message: code, retryable: false }); };
function scope(request: Parameters): RequestMap['reconciliation.preview'] {
  return { vendorId: request.vendorId, start: request.start, end: request.end,
    localBatchIds: request.localBatchIds ?? [], intervalIds: request.intervalIds ?? [] };
}

export function createMockCalibrations(applyDelta: (vendor: string, delta: string) => void,
  emit: <K extends EventName>(name: K, payload: EventMap[K]) => void) {
  const targets: Target[] = [];
  const receipts = new Map<string, { key: string; value: CommandData<'reconciliation.commitCalibration'> }>();
  let revision = 0;
  function reconcile(query: RequestMap['reconciliation.preview']): CommandData<'reconciliation.preview'> {
    const base = reconciliationPreview(query);
    const active = targets.filter(row => row.vendorId === query.vendorId && row.state === 'active'
      && row.scope.start >= query.start && row.scope.end <= query.end
      && (!query.localBatchIds?.length || row.scope.localBatchIds.every(id => query.localBatchIds!.includes(id))));
    const correction = active.reduce((sum, row) => sum + BigInt(row.deltaPico), 0n);
    return { ...base, existingCorrectionPico: correction.toString(),
      currentEffectivePico: (BigInt(base.currentEstimatePico) + correction).toString(),
      snapshot: { ...base.snapshot, cost: revision } };
  }
  function preview(request: Parameters): CommandData<'reconciliation.previewCalibration'> {
    const selected = request.targetId ? targets.find(target => target.id === request.targetId && target.state === 'active') : undefined;
    if (request.action === 'revoke' && !selected) fail('CALIBRATION_TARGET_NOT_FOUND');
    const query = selected && request.action === 'revoke' ? selected.scope : scope(request);
    const active = targets.filter(target => target.vendorId === query.vendorId && target.state === 'active'
      && target.scope.start < query.end && target.scope.end > query.start);
    const replacements = new Set([...(request.replaceTargetIds ?? []), ...(request.targetId ? [request.targetId] : [])]);
    if (active.some(target => !replacements.has(target.id))) fail('CALIBRATION_OVERLAP');
    if (active.some(target => target.scope.start < query.start || target.scope.end > query.end)) fail('CALIBRATION_PARTIAL_OVERLAP');
    if (request.action !== 'revoke' && !request.confirmLocalScope) fail('CALIBRATION_CONFIRM_SCOPE');
    if (active.length && request.action !== 'revoke' && !request.confirmReplacement) fail('CALIBRATION_CONFIRM_REPLACEMENT');
    const base = reconciliationPreview(query);
    const current = selected?.targetPico ?? active[0]?.targetPico ?? base.currentEffectivePico;
    const target = request.action === 'revoke' ? base.currentEstimatePico : request.targetPico ?? '0';
    if (BigInt(target) < 0n) fail('CALIBRATION_AMOUNT_INVALID');
    if (request.basis === 'remote' && (base.reliability !== 'reliable_usage' || target !== base.remoteTargetPico)) fail('CALIBRATION_REMOTE_UNPROVEN');
    const delta = (BigInt(target) - BigInt(current)).toString();
    const day = new Date(Date.parse(query.start) + 8 * 3600000).toISOString().slice(0, 10);
    const days = request.dayTargets?.length ? request.dayTargets : [{ day, targetPico: target }];
    if (days.reduce((sum, row) => sum + BigInt(row.targetPico), 0n) !== BigInt(target)) fail('CALIBRATION_ALLOCATION_INVALID');
    let hash = 2166136261;
    for (const char of canonicalPayload([request, revision, active])) hash = Math.imul(hash ^ char.charCodeAt(0), 16777619);
    return { ...base, currentEffectivePico: current, existingCorrectionPico: (BigInt(current) - BigInt(base.currentEstimatePico)).toString(),
      action: request.action ?? 'apply', targetPico: target, deltaPico: delta,
      days: days.map((row, index) => ({ ...row, estimatePico: index === 0 ? base.currentEstimatePico : '0',
        currentEffectivePico: index === 0 ? current : '0', deltaPico: index === 0 ? (BigInt(row.targetPico) - BigInt(current)).toString() : row.targetPico,
        unallocatedPico: (BigInt(row.targetPico) - (request.allocations ?? []).filter(part => part.day === row.day).reduce((sum, part) => sum + BigInt(part.targetPico), 0n)).toString() })),
      modelTargets: request.allocationMode === 'proportional' ? [{day,modelKey:'gpt-6',estimatePico:base.currentEstimatePico,targetPico:target,deltaPico:delta}] : [],
      modelAllocationCount: (request.allocations ?? []).filter(part => part.confirmModelCoverage).length,
      replacedTargets: active.map(row => ({ id: row.id, targetPico: row.targetPico, revision: row.revision })),
      fingerprint: (hash >>> 0).toString(16).padStart(8, '0').repeat(8),
    };
  }
  function commit(request: RequestMap['reconciliation.commitCalibration']): CommandData<'reconciliation.commitCalibration'> {
    const key = canonicalPayload(request); const saved = receipts.get(request.operationId);
    if (saved) { if (saved.key !== key) fail('OPERATION_ID_REUSED'); return structuredClone(saved.value); }
    const { operationId, fingerprint, ...parameters } = request;
    const plan = preview(parameters);
    if (plan.fingerprint !== fingerprint) fail('CALIBRATION_PLAN_CHANGED');
    const active = targets.filter(row => plan.replacedTargets.some(old => old.id === row.id));
    applyDelta(request.vendorId, plan.deltaPico);
    active.forEach(row => { row.state = plan.action === 'revoke' ? 'revoked' : 'replaced'; row.revision++; });
    const targetId = plan.action === 'revoke' ? parameters.targetId! : crypto.randomUUID();
    if (plan.action !== 'revoke') targets.unshift({ id: targetId, vendorId: request.vendorId, state: 'active',
      targetPico: plan.targetPico, estimatePico: plan.currentEstimatePico, deltaPico: (BigInt(plan.targetPico) - BigInt(plan.currentEstimatePico)).toString(),
      scope: { vendorId: parameters.vendorId, start: parameters.start, end: parameters.end, localBatchIds: parameters.localBatchIds ?? [], intervalIds: parameters.intervalIds ?? [] }, createdAt: new Date().toISOString(), revision: 1, basis: parameters.basis ?? 'manual', legacyFlowId: null,
      allocations: (parameters.allocations ?? []).map(row => ({ ...row, harnesses: row.harnesses ?? [], sessionRefs: row.sessionRefs ?? [], modelKeys: row.modelKeys ?? [], groupIds: row.groupIds ?? [], versionIds: row.versionIds ?? [], confirmModelCoverage: row.confirmModelCoverage ?? false })), dayTargets: parameters.dayTargets ?? [] });
    revision++;
    const result = { ...plan, targetId, revision };
    receipts.set(operationId, { key, value: result }); emit('cost.changed', { revision });
    return structuredClone(result);
  }
  function list(request: RequestMap['reconciliation.targets']): CommandData<'reconciliation.targets'> {
    return { targets: structuredClone(targets.filter(row => row.vendorId === request.vendorId && (!request.state || request.state === 'all' || row.state === request.state)).slice(0, 50)), nextCursor: null, revision };
  }
  return { preview, commit, list, reconcile, active: () => targets.filter(row => row.state === 'active'), revision: () => revision };
}
