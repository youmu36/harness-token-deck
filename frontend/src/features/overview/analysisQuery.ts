import type { Bridge, RequestMap } from '../../app/ports';
import { readShared } from '../../shared/readService';

export async function readAnalysis(bridge: Bridge, parameters: RequestMap['stats.explore'], signal: AbortSignal) {
  return readShared(bridge, 'stats.explore', parameters, signal);
}
