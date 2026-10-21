import type { RequestMap } from '../../app/ports';
export type ExploreScope = Pick<RequestMap['stats.explore'], 'harnesses' | 'vendorIds' | 'groupIds' | 'versionIds' | 'modelKeys'>;
export const emptyScope = (): ExploreScope => ({ harnesses: [], vendorIds: [], groupIds: [], versionIds: [], modelKeys: [] });
export const hasScope = (scope: ExploreScope) => Object.values(scope).some(value => value?.length);
