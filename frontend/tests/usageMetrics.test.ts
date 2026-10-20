import { mount } from '@vue/test-utils';
import { describe, expect, it } from 'vitest';
import { metricNotes } from '../src/features/usage-common/metricNotes';
import MetricsGrid from '../src/features/usage-common/MetricsGrid.vue';
import type { Metrics, UsageHarness } from '../src/features/usage-common/types';
import { metrics } from './usageFixtures';

describe('metric notes', () => {
  const cases: [Partial<Metrics>, UsageHarness, string, string][] = [
    [{ cacheCoverageIncomplete: true }, 'codex', 'newInput', '缓存覆盖不足，新增输入不可确定'],
    [{ missingUsageCount: 3 }, 'codex', 'output', '3 条用量缺失'],
    [{ inputTokens: 0, cacheHitRate: null }, 'codex', 'hitRate', '输入为 0，无法计算'],
    [{ cacheHitRate: null, cacheCoverageIncomplete: true }, 'codex', 'hitRate', '缓存尚未探测'],
    [{ cacheCoverageIncomplete: true }, 'codex', 'hitRate', '仅按缓存已知记录加权'],
    [{}, 'zcode', 'hitRate', '按已探测缓存加权（Grok 未探测的 0 不计入）'],
    [{}, 'codex', 'hitRate', ''],
    [{}, 'dsh', 'hitRate', ''],
    [{}, 'all', 'hitRate', ''],
    [{ unpriced: true, incomplete: true }, 'codex', 'cost', '含未定价用量'],
    [{ unpriced: true }, 'codex', 'cost', '含未定价用量'],
    [{ incomplete: true }, 'codex', 'cost', '估算成本不完整'],
    [{}, 'codex', 'cost', ''],
    [{ recordCount: 0, unknownModelCount: 5 }, 'codex', 'cacheRead', '当前范围暂无入账用量'],
    [{ unknownModelCount: 5 }, 'codex', 'totalTokens', '5 条模型未知'],
    [{}, 'codex', 'newInput', ''],
  ];
  it.each(cases)('explains %j / %s / %s', (patch, harness, key, text) => {
    expect(metricNotes(metrics(patch), harness)[key as keyof ReturnType<typeof metricNotes>]).toBe(text);
  });
  it('prioritizes missing output and unknown input over generic model notes', () => {
    const notes = metricNotes(metrics({ unknownModelCount: 5, missingUsageCount: 3, cacheCoverageIncomplete: true }), 'codex');
    expect(notes.output).toBe('3 条用量缺失'); expect(notes.newInput).toContain('新增输入不可确定');
    expect(notes.cacheRead).toBe('5 条模型未知');
  });
  it('renders six cards, unavailable new input and exact money without pricing links', () => {
    const wrapper = mount(MetricsGrid, { props: { harness: 'codex', metrics: metrics({ cacheCoverageIncomplete: true, unpriced: true, picoUsd: '9007199254740993123456' }) } });
    expect(wrapper.findAll('article')).toHaveLength(6);
    expect(wrapper.findAll('.metric__value')[0]!.text()).toBe('--');
    expect(wrapper.text()).toContain('缓存覆盖不足，新增输入不可确定');
    expect(wrapper.find('a').exists()).toBe(false);
    expect(wrapper.text()).toContain('含未定价用量');
    expect(wrapper.get('.money .money').text()).toBe('$9,007,199,254.7410');
    wrapper.unmount();
  });
});
