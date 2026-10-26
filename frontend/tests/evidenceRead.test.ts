import { flushPromises, mount } from '@vue/test-utils';
import { expect, it, vi } from 'vitest';
import { BridgeKey } from '../src/app/ports';
import { createMockBridge } from '../src/infra/bridge/mock';
import ReceiptEvidence from '../src/features/wallet/ReceiptEvidence.vue';

it('reads diagnostic pages only while expanded and releases them on collapse', async () => {
  const bridge = createMockBridge(), call = vi.spyOn(bridge, 'call');
  const wrapper = mount(ReceiptEvidence, { props: { scope: { vendorId:'example-a', start:'2026-10-01T00:00:00.000Z', end:'2026-10-02T00:00:00.000Z' } }, global: { provide: { [BridgeKey as symbol]: bridge } } });
  try {
    await flushPromises();
    const reads = () => call.mock.calls.filter(([name]) => name === 'stats.evidence');
    expect(reads()).toHaveLength(0);
    const details = wrapper.get('details');
    (details.element as HTMLDetailsElement).open = true; await details.trigger('toggle'); await flushPromises();
    expect(reads()).toHaveLength(1);
    (details.element as HTMLDetailsElement).open = false; await details.trigger('toggle'); await flushPromises();
    bridge.receiveEvent('cost.changed', { revision: 2 }); await flushPromises();
    expect(reads()).toHaveLength(1);
  } finally { wrapper.unmount(); bridge.dispose(); }
});
