import { mount } from '@vue/test-utils';
import { describe, expect, it } from 'vitest';
import UsageTrend from '../src/features/usage-common/UsageTrend.vue';
import ModelTable from '../src/features/usage-common/ModelTable.vue';
import HourlyTable from '../src/features/usage-common/HourlyTable.vue';
import { generateMockSummary } from '../src/infra/bridge/mockStats';
import { metrics, model } from './usageFixtures';

const start = '2026-10-05T00:00:00.000Z'; const end = '2026-10-06T00:00:00.000Z';
describe('usage trend and tables', () => {
  it('shows token totals as single bars, switches metric and uses the response grain', async () => {
    const summary = generateMockSummary({ harness: 'codex', start, end, offsetMinutes: 0, grain: 'day' });
    const wrapper = mount(UsageTrend, { props: { summary, start, end, metric: 'tokens', grain: 'hour' } });
    expect(wrapper.text()).toContain('实际粒度：天');
    expect(wrapper.find('.legend').exists()).toBe(false);
    expect(wrapper.findAll('.bar')).toHaveLength(summary.trend.length);
    const pointGroup = wrapper.find('.point');
    await pointGroup.trigger('focus');
    expect(wrapper.get('.chart-tooltip').text()).toContain('数值：');
    expect(wrapper.get('.chart-tooltip').text()).not.toMatch(/新增输入|缓存命中|输出/);
    const point = summary.trend[0]!;
    expect(wrapper.findAll('.sr-only tbody tr')[0]!.findAll('td').map(cell => cell.text())).toEqual([(point.inputTokens + point.outputTokens).toLocaleString('en-US'), '无']);
    await wrapper.get('[data-value="cost"]').trigger('click');
    expect(wrapper.emitted('update:metric')).toEqual([['cost']]);
    await wrapper.setProps({ metric: 'cost', summary: { ...summary, trend: [{ ...point, picoUsd: '9007199254740993123456' }] } });
    expect(wrapper.get('.sr-only tbody').text()).toContain('$9,007,199,254.7410');
    await wrapper.setProps({ metric: 'hitRate', summary: { ...summary, trend: [{ ...point, cacheHitRate: null }] } });
    expect(wrapper.findAll('.unknown')).toHaveLength(1);
    wrapper.unmount();
  });
  it('keeps hourly data available across the full 30-day range', async () => {
    const wrapper = mount(UsageTrend, { props: { start, end: '2026-10-08T00:00:00.000Z', metric: 'tokens', grain: 'hour' } });
    expect(wrapper.get('[data-value="hour"]').attributes('disabled')).toBeUndefined();
    expect(wrapper.emitted('update:grain')).toBeUndefined();
    await wrapper.setProps({ end: '2026-11-04T00:00:00.000Z' });
    expect(wrapper.get('[data-value="hour"]').attributes('disabled')).toBeUndefined();
    expect(wrapper.emitted('update:grain')).toBeUndefined();
    expect(wrapper.find('.grain-note').exists()).toBe(false);
    await wrapper.get('[data-value="hour"]').trigger('click');
    expect(wrapper.emitted('update:grain')).toHaveLength(1);
    wrapper.unmount();
  });
  it('uses two-hour bars with exact costs, weighted rates and a range tooltip across midnight', async () => {
    const request = { harness: 'all' as const, start: '2026-10-01T16:30:00.000Z', end: '2026-10-08T16:30:00.000Z', offsetMinutes: 345, grain: 'twoHours' as const };
    const summary = generateMockSummary(request);
    const wrapper = mount(UsageTrend, { props: { summary, start: request.start, end: request.end, metric: 'tokens', grain: 'hour' } });
    expect(wrapper.text()).toContain('实际粒度：2小时');
    expect(wrapper.get('[data-value="hour"]').text()).toBe('小时');
    expect(summary.trend).toHaveLength(85);
    expect(summary.trend[0]!.bucket).toBe('2026-10-01T22:00:00+05:45');
    await wrapper.find('.point').trigger('focus');
    expect(wrapper.get('[role="tooltip"] strong').text()).toBe('10-01 22:00 至 10-02 00:00');
    expect(wrapper.find('.point').attributes('aria-label')).toContain('10-01 22:00 至 10-02 00:00');
    expect(summary.trend.reduce((sum, point) => sum + point.inputTokens + point.outputTokens, 0)).toBe(summary.metrics.totalTokens);
    expect(summary.trend.reduce((sum, point) => sum + BigInt(point.picoUsd), 0n).toString()).toBe(summary.metrics.picoUsd);
    await wrapper.setProps({ metric: 'cost', summary: { ...summary, trend: [{ ...summary.trend[0]!, picoUsd: '9007199254740993123456' }] } });
    expect(wrapper.get('[role="tooltip"]').text()).toContain('$9,007,199,254.7410');
    await wrapper.setProps({ metric: 'hitRate', summary });
    expect(wrapper.get('[role="tooltip"]').text()).toContain(`${(summary.trend[0]!.cacheHitRate! * 100).toFixed(1)}%`);
    wrapper.unmount();
  });
  it('sorts model cost numerically and shows badges and the coverage footnote', async () => {
    const wrapper = mount(ModelTable, { props: { models: [
      model('B', { picoUsd: '900' }), model('A', { picoUsd: '1000', unknownModelCount: 2, cacheCoverageIncomplete: true, incomplete: true, unpriced: true }),
    ] } });
    expect(wrapper.get('[aria-sort="descending"]').text()).toContain('成本');
    const first = wrapper.findAll('tbody tr')[0]!;
    expect(first.text()).toContain('A'); expect(first.text()).toContain('模型未知'); expect(first.text()).toContain('未定价'); expect(first.text()).toContain('不完整');
    expect(first.findAll('td')[2]!.text()).toBe('--'); expect(first.findAll('td')[5]!.text()).toBe('60.0%*');
    expect(wrapper.get('.usage-footnote').text()).toContain('仅按缓存已知记录加权');
    await wrapper.get('[aria-sort="descending"] button').trigger('click');
    expect(wrapper.findAll('tbody tr')[0]!.text()).toContain('B');
    wrapper.unmount();
  });
  it('uses hourly totals for all models, distinguishes the unknown model, and resets a vanished model', async () => {
    const shared = { bucket: start, label: '10-05 00:00' };
    const wrapper = mount(HourlyTable, { props: {
      models: [model(''), model('A')], modelValue: null,
      hourlyTotals: [{ ...shared, displayName: '全部模型', metrics: metrics({ newInput: 70 }) }],
      hourly: [{ ...shared, modelKey: '', displayName: '模型未知', metrics: metrics({ newInput: 30, cacheCoverageIncomplete: true }) }, { ...shared, modelKey: 'A', displayName: 'A', metrics: metrics() }],
    } });
    expect(wrapper.get('tbody').text()).toContain('70');
    await wrapper.get('select').setValue('0'); expect(wrapper.emitted('update:modelValue')).toEqual([['']]);
    await wrapper.setProps({ modelValue: '' });
    expect(wrapper.get('tbody').text()).toContain('模型未知'); expect(wrapper.find('.usage-footnote').exists()).toBe(true);
    await wrapper.setProps({ models: [model('A')] });
    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual([null]);
    expect(wrapper.get('select').element.value).toBe('-1');
    expect(wrapper.get('tbody').text()).toContain('全部模型');
    await wrapper.setProps({ modelValue: 'A' }); expect(wrapper.find('.usage-footnote').exists()).toBe(false);
    wrapper.unmount();
  });
  it('preserves a persisted selection while data is loading and resets it after an empty response', async () => {
    const wrapper = mount(HourlyTable, { props: { models: [], hourly: [], hourlyTotals: [], modelValue: 'A', loading: true } });
    expect(wrapper.emitted('update:modelValue')).toBeUndefined();
    await wrapper.setProps({ loading: false }); expect(wrapper.emitted('update:modelValue')).toEqual([[null]]);
    wrapper.unmount();
  });
  it('virtualizes long hourly lists in chronological order', async () => {
    const hourlyTotals = Array.from({ length: 240 }, (_, index) => {
      const at = new Date(Date.parse(start) + index * 3_600_000).toISOString();
      return { bucket: at, label: at, displayName: '全部模型', metrics: metrics() };
    }).reverse();
    const wrapper = mount(HourlyTable, { props: { models: [], hourly: [], hourlyTotals, modelValue: null } });
    expect(wrapper.findAll('tbody tr').length).toBeLessThan(30);
    expect(wrapper.get('[aria-rowindex="2"]').text()).toContain(start);
    const scroller = wrapper.get('.table-wrap'); scroller.element.scrollTop = 200 * 40; await scroller.trigger('scroll');
    expect(wrapper.find('[aria-rowindex="2"]').exists()).toBe(false);
    expect(wrapper.find('[aria-rowindex="202"]').exists()).toBe(true);
    wrapper.unmount();
  });
});
