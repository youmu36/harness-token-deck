import type { DeepReadonly } from 'vue';
import type { CommandData } from '../../app/ports';

export type Harness = 'claude' | 'codex' | 'zcode' | 'dsh';
export type Catalog = DeepReadonly<CommandData<'pricing.catalog'>>;
export type PricingVersion = Catalog['groups'][number]['versions'][number];
export type PricingSelection = { groupId: string; versionId: string };
export const harnessNames: Record<Harness, string> = { claude: 'Claude Code', codex: 'Codex', zcode: 'Zcode', dsh: 'Deepseek Harness' };
export function versionName(version: Pick<PricingVersion, 'label' | 'number'>) { return version.label || `v${version.number}`; }
