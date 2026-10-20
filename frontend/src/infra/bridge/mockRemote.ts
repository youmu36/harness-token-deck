import { AppError } from '../../app/errors';
import type { CommandData, RequestMap } from '../../app/ports';
import { canonicalPayload } from '../../shared/payload';
type Snapshot = CommandData<'remote.get'>;
type Settings = RequestMap['remote.test'];
type Write = 'remote.save' | 'remote.refresh' | 'remote.delete';
export function createMockRemote(wallets: { setSite: (id: string, url: string) => void; list: (includeArchived?: boolean) => CommandData<'wallet.list'>; remoteChanged: (vendorId: string, observation?: { amountPico: string; fetchedAt: string }) => number; operationUsed: (id: string) => boolean }) {
  const accounts = new Map<string, NonNullable<Snapshot['account']>>();
  const observations = new Map<string, NonNullable<Snapshot['observation']>>();
  const receipts = new Map<string, { fingerprint: string; result: Snapshot }>();
  function get(vendorId: string): Snapshot {
    const account = accounts.get(vendorId) ?? null; const observation = observations.get(vendorId) ?? null;
    const stale = observation ? Date.now() - Date.parse(observation.fetchedAt) > (account?.refreshMinutes ?? 5) * 120_000 : false;
    return structuredClone({ account, observation: observation ? { ...observation, stale, canCalibrate: observation.canCalibrate && !stale } : null, status: !account ? 'unconfigured' : stale ? 'stale' : observation ? 'ok' : 'pending', error: null, nextRefreshAt: account?.enabled ? new Date(Date.now() + account.refreshMinutes * 60_000).toISOString() : null, refreshing: false, revision: wallets.list().revision });
  }
  function test(request: Settings): CommandData<'remote.test'> {
    if (!['newapi', 'sub2api', 'deepseek'].includes(request.siteType)) throw new AppError({ code: 'REMOTE_SITE_REQUIRED', message: '请选择接口类型', retryable: false });
    if (request.siteType === 'sub2api' && request.authMode !== 'api_key') throw new AppError({ code: 'REMOTE_ACCOUNT_INVALID', message: 'sub2api 须使用 API 令牌', retryable: false });
    const saved = accounts.get(request.vendorId);
    if (!request.credential && !saved?.maskedCredential) throw new AppError({ code: 'REMOTE_CREDENTIAL_REQUIRED', message: '请填写令牌', retryable: false });
    let url: URL; try { url = new URL(request.baseUrl); } catch { throw new AppError({ code: 'REMOTE_ACCOUNT_INVALID', message: '站点地址无效', retryable: false }); }
    if (url.username || url.password || url.search || url.hash || !['https:', 'http:'].includes(url.protocol) || url.protocol === 'http:' && !['localhost', '127.0.0.1'].includes(url.hostname) || request.authMode === 'access_token' && (!request.userId || BigInt(request.userId) <= 0n) || request.siteType === 'newapi' && request.authMode !== 'access_token' || request.siteType === 'sub2api' && request.authMode === 'access_token') throw new AppError({ code: 'REMOTE_ACCOUNT_INVALID', message: '请检查账户设置', retryable: false });
    const kind = ['sub2api', 'deepseek'].includes(request.siteType) ? 'account_remaining' : request.credential === 'unlimited-test' ? 'unlimited' : request.credential === 'quota-only-test' ? 'quota_units' : request.authMode === 'billing' ? 'subscription_remaining' : request.authMode === 'access_token' ? 'account_remaining' : 'token_remaining';
    return { observation: { accountId: saved?.id ?? 'mock-test', fetchedAt: new Date().toISOString(), kind, source: url.hostname, currency: ['token_remaining', 'account_remaining'].includes(kind) ? 'USD' : null, amountPico: ['unlimited', 'quota_units'].includes(kind) ? null : '1975310000001', rawQuota: '987655', quotaPerUnit: kind === 'quota_units' ? null : '500000', cumulativeUsedPico: null, usageScope: null, originalCurrency: null, conversionRule: null, stale: false, currencyUncertain: kind === 'subscription_remaining', displayInCurrency: true, canCalibrate: ['token_remaining', 'account_remaining'].includes(kind) }, error: null };
  }
  function change(command: Write, request: RequestMap[Write]): Snapshot {
    if (wallets.operationUsed(request.operationId)) throw new AppError({ code: 'OPERATION_ID_REUSED', message: '操作标识已使用', retryable: false });
    const fingerprint = canonicalPayload([command, request]); const prior = receipts.get(request.operationId);
    if (prior) { if (prior.fingerprint !== fingerprint) throw new AppError({ code: 'OPERATION_ID_REUSED', message: '操作标识已使用', retryable: false }); return structuredClone(prior.result); }
    const vendor = wallets.list(true).vendors.find(v => v.id === request.vendorId);
    if (!vendor || vendor.status !== 'active') throw new AppError({ code: 'VENDOR_NOT_FOUND', message: '供应商不存在或已归档', retryable: false });
    if (command === 'remote.save' && 'baseUrl' in request) {
      test(request); const previous = accounts.get(request.vendorId);
      const secret = request.credential;
      accounts.set(request.vendorId, { id: previous?.id ?? crypto.randomUUID(), vendorId: request.vendorId, baseUrl: request.baseUrl, siteType: request.siteType ?? 'auto', authMode: request.authMode ?? 'api_key', userId: request.userId ?? null, quotaPerUnit: request.quotaPerUnit ?? null, refreshMinutes: request.refreshMinutes ?? 5, enabled: request.enabled ?? true, maskedCredential: secret ? secret.length > 4 ? '••••' + secret.slice(-4) : '••••' : previous?.maskedCredential ?? null });
      wallets.setSite(request.vendorId, request.baseUrl);
      observations.delete(request.vendorId);
    } else if (command === 'remote.delete') { accounts.delete(request.vendorId); observations.delete(request.vendorId); }
    else { const a = accounts.get(request.vendorId); if (!a) throw new AppError({ code: 'REMOTE_ACCOUNT_NOT_FOUND', message: '请先配置远程账户', retryable: false }); if (a.siteType === 'auto') throw new AppError({ code: 'REMOTE_SITE_REQUIRED', message: '请选择接口类型', retryable: false }); observations.set(request.vendorId, test({ ...a, siteType: a.siteType, credential: null }).observation!); }
    const observed = observations.get(request.vendorId);
    wallets.remoteChanged(request.vendorId, command === 'remote.refresh' && observed?.canCalibrate && observed.amountPico !== null ? { amountPico: observed.amountPico, fetchedAt: observed.fetchedAt } : undefined);
    const result = get(request.vendorId); receipts.set(request.operationId, { fingerprint, result: structuredClone(result) }); return result;
  }
  function resolve(vendorId: string, at: string, target: bigint): bigint {
    const observation = get(vendorId).observation;
    if (!observation?.canCalibrate || observation.fetchedAt !== at || observation.amountPico === null || BigInt(observation.amountPico) / 1_000_000n * 1_000_000n !== target) throw new AppError({ code: 'REMOTE_OBSERVATION_CHANGED', message: '远程观测已变化', retryable: false });
    return BigInt(observation.amountPico);
  }
  return { get, test, change, resolve, operationUsed: (id: string) => receipts.has(id) };
}
