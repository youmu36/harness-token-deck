import { AppError } from '../../app/errors';
import type { CommandData, EventMap, EventName, RequestMap } from '../../app/ports';
import type { Entry, WalletChangeResponse } from '../generated/contracts';
import { canonicalPayload } from '../../shared/payload';
type WalletWrite = 'wallet.createVendor' | 'wallet.renameVendor' | 'wallet.topUp' | 'wallet.calibrate' | 'wallet.linkGroups' | 'wallet.deleteVendor' | 'wallet.restoreVendor' | 'wallet.setVisibility';
function website(text?: string | null): string | null {
  if (!text?.trim()) return null;
  try {
    const url = new URL(text.trim());
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw new Error();
    return url.origin + url.pathname.replace(/\/$/, '').replace(/\/v1$/, '');
  } catch { throw new AppError({ code: 'VENDOR_URL_INVALID', message: '供应商网址无效', retryable: false }); }
}
function pico(text: string) {
  if (!/^-?[0-9]{1,9}(?:\.[0-9]{1,6})?$/.test(text)) throw new AppError({ code: 'AMOUNT_INVALID', message: '金额无效', retryable: false });
  const [whole, fraction = ''] = text.replace(/^-/, '').split('.');
  const value = BigInt(whole!) * 1_000_000_000_000n + BigInt(fraction.padEnd(12, '0'));
  return text.startsWith('-') ? -value : value;
}
export function createMockWallet(state: CommandData<'wallet.list'>, catalog: CommandData<'pricing.catalog'>, emit: <K extends EventName>(name: K, payload: EventMap[K]) => void, pricingOperationUsed: (id: string) => boolean) {
  let seq = 2;
  const entries = new Map<string, Entry[]>(state.vendors.map(v => [v.id, [
    { id: `${v.id}-topup`, kind: 'top_up', seq: 1, amountPico: v.R, occurredAt: v.createdAt, count: 1, note: '', calibration: null, noteDetails: null },
    { id: null, kind: 'consumption_daily', seq: 2, amountPico: v.C, occurredAt: '2026-10-05T00:00:00.000Z', count: 24, note: '', calibration: null, noteDetails: null },
  ]]));
  const receipts = new Map<string, { fingerprint: string; result: WalletChangeResponse }>();
  let remoteResolver: ((vendorId: string, at: string, target: bigint) => bigint) | undefined;
  function list(includeArchived = false) { return structuredClone({ ...state, vendors: state.vendors.filter(v => includeArchived || v.status === 'active') }); }
  function accountPage(request:RequestMap['wallet.accountEntries']):CommandData<'wallet.accountEntries'> {
    const vendor=state.vendors.find(row=>row.id===request.vendorId);
    const wallet=request.scope==='remote'?vendor?.remoteWallet:vendor?.localWallet??vendor;
    if(!wallet || request.cursor)return {entries:[],nextCursor:null,revision:state.revision};
    const stamp=new Date(Date.now()-86400_000).toISOString();
    const common={occurredAt:stamp,postedAt:stamp,originalId:null,intervalId:null,availablePico:'0',fromAt:null,toAt:null,entryRevision:1,manuallyCalibrated:false,estimatedConsumptionPico:null,noticeId:null};
    const rows:CommandData<'wallet.accountEntries'>['entries']=[
      {...common,id:`${request.vendorId}-flow-c`,kind:'consumption',R:'0',C:wallet.C,A:'0',beforeR:wallet.R,afterR:wallet.R,beforeC:'0',afterC:wallet.C,beforeB:wallet.R,afterB:wallet.B,note:'示例：静默批次合并消费'},
      {...common,id:`${request.vendorId}-flow-r`,kind:'top_up',R:wallet.R,C:'0',A:'0',beforeR:'0',afterR:wallet.R,beforeC:'0',afterC:'0',beforeB:'0',afterB:wallet.R,note:'示例：人工充值'},
    ];
    return {entries:rows.filter(row=>(row.R!=='0'||row.C!=='0')&&(!request.kind||request.kind==='all'||row.kind===request.kind)&&(!request.startDay||stamp.slice(0,10)>=request.startDay)&&(!request.endDay||stamp.slice(0,10)<=request.endDay)),nextCursor:null,revision:state.revision};
  }
  function page(request: RequestMap['wallet.entries']): CommandData<'wallet.entries'> {
    if (!entries.has(request.vendorId)) throw new AppError({ code: 'VENDOR_NOT_FOUND', message: '供应商不存在', retryable: false });
    const raw = entries.get(request.vendorId)!;
    const daily = new Map<string, Entry>(); const visible: Entry[] = []; const offset = (request.offsetMinutes ?? 0) * 60000;
    for (const row of raw) {
      if (row.kind !== 'consumption_daily') { if (row.note !== 'calibration' || BigInt(row.amountPico) !== 0n) visible.push(row); continue; }
      const day = new Date(Date.parse(row.occurredAt) + offset).toISOString().slice(0, 10); const old = daily.get(day);
      daily.set(day, { ...row, id: null, seq: Math.min(row.seq, old?.seq ?? row.seq), amountPico: (BigInt(old?.amountPico ?? '0') + BigInt(row.amountPico)).toString(), occurredAt: new Date(Date.parse(day + 'T00:00:00.000Z') - offset).toISOString(), count: (old?.count ?? 0) + row.count, note: 'original_spending', noteDetails: null });
    }
    visible.push(...daily.values());
    const selected = visible.filter(row => (request.cursor == null || row.seq < request.cursor) && (!request.kind || request.kind === 'all' || row.kind === request.kind)).sort((a, b) => b.seq - a.seq);
    const rows = selected.slice(0, request.limit ?? 100);
    return structuredClone({ entries: rows, nextCursor: selected.length > rows.length ? rows.at(-1)!.seq : null, revision: state.revision });
  }
  function change(command: WalletWrite, request: RequestMap[WalletWrite]): WalletChangeResponse {
    if (pricingOperationUsed(request.operationId)) throw new AppError({ code: 'OPERATION_ID_REUSED', message: '操作标识已使用', retryable: false });
    const fingerprint = canonicalPayload([command, request]); const prior = receipts.get(request.operationId);
    if (prior) { if (prior.fingerprint !== fingerprint) throw new AppError({ code: 'OPERATION_ID_REUSED', message: '操作标识已使用', retryable: false }); return structuredClone(prior.result); }
    const candidate = structuredClone(state); const pricing = structuredClone(catalog);
    const id = 'vendorId' in request ? request.vendorId : crypto.randomUUID();
    let vendor = candidate.vendors.find(v => v.id === id); let action: WalletChangeResponse['action'] = null;
    let appended: Entry | undefined; let changed = true; const affected = new Set([id]);
    const fail = (code: string) => { throw new AppError({ code, message: code, retryable: false, ...(code === 'WALLET_REVISION_CONFLICT' ? { conflictRevision: state.revision } : {}) }); };
    const checkName = (name: string) => { if (!name || name.length > 60) fail('WALLET_INVALID'); if (candidate.vendors.some(v => v.id !== id && v.status === 'active' && v.name === name)) fail('VENDOR_NAME_CONFLICT'); };
    if (command === 'wallet.createVendor' && 'name' in request) {
      checkName(request.name.trim()); if (candidate.vendors.length >= 200) fail('VENDOR_LIMIT');
      const at = new Date().toISOString(); const initial = 'initialBalance' in request && request.initialBalance ? pico(request.initialBalance) : undefined;
      vendor = { id, name: request.name.trim(), url: website(request.url), status: 'active', createdAt: at, R: '0', pastTopUpPico: pico('pastTopUp' in request ? request.pastTopUp ?? '0' : '0').toString(), pastConsumptionPico: pico('pastConsumption' in request ? request.pastConsumption ?? '0' : '0').toString(), C: '0', A: (initial ?? 0n).toString(), B: (initial ?? 0n).toString(), lastCalibratedAt: initial === undefined ? null : at, linkedGroups: [], coverageIncomplete: false, unpricedCount: 0, incompleteCount: 0, historyCoverageStart: null, account: null, localWallet: null, remoteWallet: null, walletRevision: state.revision, walletVisibility: 'both' };
      candidate.vendors.push(vendor);
      if (initial !== undefined) appended = { id: crypto.randomUUID(), kind: 'adjustment', seq: seq + 1, amountPico: initial.toString(), occurredAt: at, count: 1, note: 'calibration', noteDetails: null, calibration: { targetPico: initial.toString(), observedBalancePico: '0', watermarkSeq: seq } };
    } else {
      if (!vendor) fail('VENDOR_NOT_FOUND');
      const v = vendor!;
      if (command !== 'wallet.restoreVendor' && command !== 'wallet.setVisibility' && v.status === 'archived') fail('VENDOR_ARCHIVED');
      if (command === 'wallet.setVisibility' && 'mode' in request) {changed = (v.walletVisibility ?? 'both') !== request.mode; v.walletVisibility = request.mode;}
      if (command === 'wallet.renameVendor' && 'name' in request) { checkName(request.name.trim()); changed = v.name !== request.name.trim(); v.name = request.name.trim(); if ('pastTopUp' in request && request.pastTopUp != null) { const value = pico(request.pastTopUp); if (value < 0n) fail('AMOUNT_INVALID'); changed ||= v.pastTopUpPico !== value.toString(); v.pastTopUpPico = value.toString(); } if ('pastConsumption' in request && request.pastConsumption != null) { const value = pico(request.pastConsumption); if (value < 0n) fail('AMOUNT_INVALID'); changed ||= v.pastConsumptionPico !== value.toString(); v.pastConsumptionPico = value.toString(); } if ('url' in request) { changed ||= v.url !== request.url; v.url = website(request.url); } }
      if (command === 'wallet.topUp' && 'amount' in request) {
        const value = pico(request.amount); if (value <= 0n) fail('AMOUNT_INVALID'); v.R = (BigInt(v.R) + value).toString();
        appended = { id: crypto.randomUUID(), kind: 'top_up', seq: seq + 1, amountPico: value.toString(), occurredAt: new Date().toISOString(), count: 1, note: '', calibration: null, noteDetails: null };
      }
      if (command === 'wallet.calibrate' && 'target' in request) {
        if (request.expectedWalletRevision !== state.revision) fail('WALLET_REVISION_CONFLICT');
        const target = request.remoteFetchedAt ? remoteResolver!(request.vendorId, request.remoteFetchedAt, pico(request.target)) : pico(request.target); const delta = target - BigInt(v.B); const at = new Date().toISOString(); v.A = (BigInt(v.A) + delta).toString(); v.lastCalibratedAt = at;
        appended = { id: crypto.randomUUID(), kind: 'adjustment', seq: seq + 1, amountPico: delta.toString(), occurredAt: at, count: 1, note: 'calibration', noteDetails: null, calibration: { targetPico: target.toString(), observedBalancePico: v.B, watermarkSeq: seq } };
      }
      if (command === 'wallet.linkGroups' && 'groupIds' in request) {
        if (request.expectedRevision !== state.revision) fail('WALLET_REVISION_CONFLICT');
        if (request.groupIds.some(id => !pricing.groups.some(g => g.id === id))) fail('GROUP_NOT_FOUND');
        changed = canonicalPayload([...v.linkedGroups].sort()) !== canonicalPayload([...new Set(request.groupIds)].sort());
        for (const g of pricing.groups) {
          if (request.groupIds.includes(g.id) && g.vendorId !== v.id) { if (g.vendorId) affected.add(g.vendorId); g.vendorId = v.id; }
          else if (g.vendorId === v.id && !request.groupIds.includes(g.id)) g.vendorId = null;
        }
      }
      if (command === 'wallet.deleteVendor') { action = (entries.get(id)?.length ?? 0) ? 'archive' : 'delete'; if (action === 'archive') v.status = 'archived'; else candidate.vendors = candidate.vendors.filter(item => item.id !== id); for (const g of pricing.groups) if (g.vendorId === id) g.vendorId = null; }
      if (command === 'wallet.restoreVendor') { if (v.status !== 'archived') fail('WALLET_INVALID'); checkName(v.name); v.status = 'active'; }
      v.B = (BigInt(v.R) - BigInt(v.C) + BigInt(v.A)).toString();
    }
    const associationChanged = canonicalPayload(catalog.groups.map(g => g.vendorId)) !== canonicalPayload(pricing.groups.map(g => g.vendorId));
    if (changed) candidate.revision++;
    for (const v of candidate.vendors) { v.walletRevision = candidate.revision; v.linkedGroups = pricing.groups.filter(g => g.vendorId === v.id).map(g => g.id); }
    if (associationChanged) { pricing.revision++; Object.assign(catalog, pricing); }
    Object.assign(state, candidate);
    if (!entries.has(id)) entries.set(id, []); if (appended) { seq++; entries.get(id)!.push(appended); } if (action === 'delete') entries.delete(id);
    const result: WalletChangeResponse = { changed, revision: state.revision, pricingRevision: catalog.revision, vendorId: id, vendor: state.vendors.find(v => v.id === id) ?? null, action };
    receipts.set(request.operationId, { fingerprint, result: structuredClone(result) });
    if (changed) emit('wallet.changed', { revision: state.revision, vendorIds: [...affected], catalogChanged: ['wallet.createVendor', 'wallet.renameVendor', 'wallet.deleteVendor', 'wallet.restoreVendor'].includes(command) });
    if (associationChanged) emit('pricing.changed', { revision: catalog.revision });
    return structuredClone(result);
  }
  function providerCandidates() {
    return ['https://provider-a.example', 'https://provider-b.example'].map((identifier, index) => ({ id: (index ? 'b' : 'a').repeat(64), identifier, label: null, url: identifier, fromCurrentConfig: false, vendorId: null })).filter(row => !state.vendors.some(v => v.url === row.url));
  }
  function createScanned(request: RequestMap['wallet.createScannedVendors'], candidates: ReturnType<typeof providerCandidates>): WalletChangeResponse {
    const fingerprint = canonicalPayload(['wallet.createScannedVendors', request]); const old = receipts.get(request.operationId);
    const fail = (code: string): never => { throw new AppError({ code, message: code, retryable: false }); };
    if (old) { if (old.fingerprint !== fingerprint) return fail('OPERATION_ID_REUSED'); return structuredClone(old.result); }
    const names = request.providers.map(row => row.name.trim());
    if (names.some(name => !name || name.length > 60)) return fail('WALLET_INVALID');
    if (new Set(names).size !== names.length || state.vendors.some(v => v.status === 'active' && names.includes(v.name))) return fail('VENDOR_NAME_CONFLICT');
    if (state.vendors.length + names.length > 200) return fail('VENDOR_LIMIT');
    const chosen = request.providers.map(row => candidates.find(candidate => candidate.id === row.id) ?? fail('PROVIDER_SCAN_INVALID'));
    if (chosen.some(row => state.vendors.some(v => v.url === row.url)) || new Set(chosen.map(row => row.id)).size !== chosen.length) return fail('PROVIDER_ALREADY_SAVED');
    const next = structuredClone(state); next.revision++;
    const ids = chosen.map((row, index) => {
      const id = crypto.randomUUID(); entries.set(id, []);
      next.vendors.push({ id, name: names[index]!, url: row.url, status: 'active', createdAt: new Date().toISOString(), R: '0', pastTopUpPico: '0', pastConsumptionPico: '0', C: '0', A: '0', B: '0', lastCalibratedAt: null, linkedGroups: [], coverageIncomplete: false, unpricedCount: 0, incompleteCount: 0, historyCoverageStart: null, account: null, localWallet: null, remoteWallet: null, walletRevision: next.revision, walletVisibility: 'both' }); return id;
    });
    for (const vendor of next.vendors) vendor.walletRevision = next.revision;
    Object.assign(state, next);
    const result: WalletChangeResponse = { vendor: next.vendors.at(-1)!, vendorId: ids.at(-1)!, action: null, changed: true, revision: next.revision, pricingRevision: catalog.revision };
    receipts.set(request.operationId, { fingerprint, result: structuredClone(result) }); emit('wallet.changed', { vendorIds: ids, revision: state.revision, catalogChanged: true }); return structuredClone(result);
  }
  function recalc(deltas: CommandData<'maintenance.previewRecalc'>['vendors']) {
    if (!deltas.length) return;
    state.revision++;
    for (const delta of deltas) {
      const vendor = state.vendors.find(v => v.id === delta.vendorId)!;
      vendor.C = (BigInt(vendor.C) + BigInt(delta.consumptionDelta)).toString();
      vendor.B = (BigInt(vendor.B) + BigInt(delta.balanceDelta)).toString();
      if (vendor.localWallet) {
        vendor.localWallet.C = (BigInt(vendor.localWallet.C) + BigInt(delta.consumptionDelta)).toString();
        vendor.localWallet.B = (BigInt(vendor.localWallet.B) + BigInt(delta.balanceDelta)).toString();
      }
      vendor.A = (BigInt(vendor.B) - BigInt(vendor.R) + BigInt(vendor.C)).toString();
      vendor.walletRevision = state.revision;
      entries.get(vendor.id)!.push({ id: null, kind: 'adjustment', seq: ++seq, amountPico: delta.balanceDelta, occurredAt: new Date().toISOString(), count: 1, note: 'recalc', calibration: null, noteDetails: null });
    }
    emit('wallet.changed', { revision: state.revision, vendorIds: deltas.map(v => v.vendorId) });
  }
  function remoteChanged(vendorId: string, observation?: { amountPico: string; fetchedAt: string }) {
    const vendor = state.vendors.find(v => v.id === vendorId);
    if (vendor && observation) {
      const delta = BigInt(observation.amountPico) - BigInt(vendor.B);
      if (delta > 1_000_000_000_000n) {
        entries.get(vendorId)!.push({ id: crypto.randomUUID(), kind: 'top_up', seq: ++seq, amountPico: delta.toString(), occurredAt: observation.fetchedAt, count: 1, note: '远程余额增加，自动补记充值', calibration: null, noteDetails: null });
        vendor.R = (BigInt(vendor.R) + delta).toString();
      }
      const adjustment = delta > 1_000_000_000_000n || (delta < 10_000_000_000n && delta > -10_000_000_000n) ? 0n : delta;
      entries.get(vendorId)!.push({ id: crypto.randomUUID(), kind: 'adjustment', seq: ++seq, amountPico: adjustment.toString(), occurredAt: observation.fetchedAt, count: 1, note: 'calibration', noteDetails: null, calibration: { targetPico: observation.amountPico, observedBalancePico: delta > 0n ? observation.amountPico : vendor.B, watermarkSeq: seq - 1 } });
      vendor.A = (BigInt(vendor.A) + adjustment).toString(); vendor.B = (BigInt(vendor.R) - BigInt(vendor.C) + BigInt(vendor.A)).toString(); vendor.lastCalibratedAt = observation.fetchedAt;
    }
    state.revision++; for (const v of state.vendors) v.walletRevision = state.revision;
    emit('wallet.changed', { vendorIds: [vendorId], revision: state.revision }); return state.revision;
  }
  return { list, page, accountPage, change, recalc, setSite: (id: string, url: string) => { const vendor = state.vendors.find(v => v.id === id); if (vendor) vendor.url = website(url); }, providerCandidates, createScanned, remoteChanged, setRemoteResolver: (resolver: NonNullable<typeof remoteResolver>) => { remoteResolver = resolver; }, operationUsed: (id: string) => receipts.has(id) };
}
