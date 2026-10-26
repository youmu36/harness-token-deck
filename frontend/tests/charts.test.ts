import { mount } from '@vue/test-utils';
import { afterEach, describe, expect, test } from 'vitest';
import BarChart from '../src/ui/charts/BarChart.vue';
import { createAxis, formatTick, tickIndices, type ChartPoint } from '../src/ui/charts/axis';
const cleanup: (() => void)[] = [];
afterEach(() => { cleanup.splice(0).forEach(fn => fn()); });
const points: ChartPoint[] = [
  { label: '10-06 12:00', value: null, flags: { cacheLoss: true } },
  { label: '10-06 12:01', value: 0, flags: { cacheBust: true } },
  { label: '10-06 12:02', value: 0.75, flags: { cacheBust: true, cacheLoss: true } },
];
function render(data = points, yFormat: 'tokens' | 'percent' = 'percent') {
  const wrapper = mount(BarChart, { props: { points: data, yFormat, label: '测试图表', height: 280 }, attachTo: document.body });
  cleanup.push(() => wrapper.unmount()); return wrapper;
}
describe('chart axes', () => {
  test('confirmed compaction keeps the warning bar and names the exact cause', async () => {
    const wrapper = render([{label:'压缩发生',value:0,flags:{contextCompacted:true}}]);
    expect(wrapper.find('rect.cache-bust').exists()).toBe(true);
    await wrapper.get('.point').trigger('focus');
    expect(wrapper.get('[role="tooltip"]').text()).toContain('上下文压缩：来源已明确标记');
    expect(wrapper.get('[role="tooltip"]').text()).not.toContain('缓存击穿：');
    expect(wrapper.get('tbody').text()).toContain('上下文压缩');
  });
  test('percent stays at 0–100%; tokens and pico cost use required abbreviations', () => {
    expect(createAxis(points, 'percent')).toMatchObject({ min: 0, max: 1, ticks: [0, 0.25, 0.5, 0.75, 1] });
    expect(formatTick(12000, 'tokens')).toBe('1.2万');
    expect(formatTick(123000000, 'tokens')).toBe('1.23亿');
    expect([0, 120000000000, 1200000000000, 120000000000000].map(n => formatTick(n, 'cost'))).toEqual(['$0', '$0.12', '$1.2', '$120']);
    expect(tickIndices(1500, 6)).toEqual([0, 300, 600, 899, 1199, 1499]);
    expect(createAxis([], 'tokens').max).toBeGreaterThan(0);
  });
  test('stacked positive and negative bounds fit every segment', () => {
    const scale = createAxis([{ label: '一', value: 30, segments: [{ label: '缺失输入', value: -40 }, { label: '缓存', value: 70 }] }], 'tokens');
    expect(scale.min).toBeLessThanOrEqual(-40); expect(scale.max).toBeGreaterThanOrEqual(70);
  });
});
describe('SVG bar chart', () => {
  test('blank minutes create no per-point nodes while preserving time positions, reader data and keyboard navigation', async () => {
    const data:ChartPoint[]=Array.from({length:60},(_,index)=>({label:String(index),value:null}));
    const empty=render(data);expect(empty.findAll('[data-point]')).toHaveLength(0);
    expect(empty.findAll('tbody tr')).toHaveLength(60);
    const active=render(data.map((point,index)=>({...point,value:index===30||index===59 ? .5 : null})));
    expect(active.findAll('[data-point]')).toHaveLength(2);
    expect(active.get('[tabindex="0"]').attributes('data-point')).toBe('30');
    await active.get('[data-point="30"]').trigger('keydown',{key:'ArrowRight'});
    expect(document.activeElement).toBe(active.get('[data-point="59"]').element);
  });
  test('renders the original layered bars while retaining chart semantics', () => {
    const wrapper = render([{ label: '一', value: 12000 }], 'tokens');
    expect(wrapper.findAll('foreignObject.bar')).toHaveLength(1);
    expect(wrapper.find('.bar .trend-fill').exists()).toBe(true);
    expect(wrapper.find('rect.bar').exists()).toBe(false);
    expect(wrapper.get('[role=graphics-symbol]').attributes('aria-label')).toContain('12,000');
  });
  test('unknown and zero have no placeholder bars; warnings and reader table retain their values', () => {
    const wrapper = render();
    expect(wrapper.findAll('.unknown')).toHaveLength(0);
    expect(wrapper.find('[data-point="0"] .bar').exists()).toBe(false);
    expect(wrapper.find('[data-point="1"] .bar').exists()).toBe(false);
    expect(wrapper.findAll('rect.cache-bust')).toHaveLength(2);
    expect(wrapper.findAll('rect.cache-loss')).toHaveLength(1);
    expect(wrapper.find('.legend').exists()).toBe(false);
    expect(wrapper.get('table').text()).toContain('--（未知）大量未命中');
    expect(wrapper.get('table').text()).toContain('0.0%缓存击穿');
    expect(wrapper.get('table').text()).toContain('75.0%缓存击穿');
  });
  test('one Tab entry, arrows/Home/End move focus and show warnings; Escape closes tooltip', async () => {
    const wrapper = render();
    expect(wrapper.findAll('[tabindex="0"]')).toHaveLength(1);
    const first = wrapper.get('[data-point="0"]');
    (first.element as SVGGElement).focus(); await first.trigger('focus');
    expect(wrapper.get('[role="tooltip"]').text()).toContain('--（未知）');
    await first.trigger('keydown', { key: 'ArrowRight' });
    await wrapper.get('[data-point="1"]').trigger('focus');
    expect(document.activeElement).toBe(wrapper.get('[data-point="1"]').element);
    expect(wrapper.get('[role="tooltip"]').text()).toContain('缓存击穿：上下文 ≥ 5 万且缓存命中率低于 10%');
    await wrapper.get('[data-point="1"]').trigger('keydown', { key: 'End' });
    await wrapper.get('[data-point="2"]').trigger('focus');
    expect(wrapper.get('[data-point="2"]').attributes('aria-describedby')).toBe(wrapper.get('[role="tooltip"]').attributes('id'));
    expect(wrapper.get('[role="tooltip"]').text()).not.toContain('大量未命中');
    await wrapper.get('[data-point="2"]').trigger('keydown', { key: 'Home' });
    expect(document.activeElement).toBe(first.element);
    await first.trigger('keydown', { key: 'Escape' });
    expect(wrapper.find('[role="tooltip"]').exists()).toBe(false);
  });
  test('reader table retains full stack values and exact cost label', () => {
    const wrapper = render([{ label: '一', value: 123456, valueLabel: '$0.1234', segments: [{ label: '缓存', value: 123450 }, { label: '输出', value: 6 }] }], 'tokens');
    expect(wrapper.findAll('rect.stack-segment')).toHaveLength(2);
    expect(wrapper.findAll('foreignObject.bar')).toHaveLength(0);
    expect(wrapper.get('tbody').text()).toContain('一$0.1234123,4506无');
  });
  test('hover reveals exact values only over bars and closes over gaps, axes and mouse leave', async () => {
    const wrapper = render([{ label: '一', value: 12345 }, { label: '二', value: 67890 }], 'tokens');
    const svg = wrapper.get('svg');
    Object.defineProperty(svg.element, 'getBoundingClientRect', { value: () => ({ left: 100, top: 50, width: 380, height: 140 }) });
    const first = wrapper.get('[data-point="0"] .bar');
    const center = Number(first.attributes('x')) + Number(first.attributes('width')) / 2;
    const move = (x: number, y: number) => svg.trigger('mousemove', { clientX: 100 + x / 2, clientY: 50 + y / 2 });
    expect(wrapper.find('[role="tooltip"]').exists()).toBe(false);
    await move(center, 230);
    expect(wrapper.get('[role="tooltip"]').text()).toContain('12,345');
    await move(center, 20);
    expect(wrapper.find('[role="tooltip"]').exists()).toBe(false);
    await move(407, 230);
    expect(wrapper.find('[role="tooltip"]').exists()).toBe(false);
    await move(center, 270);
    expect(wrapper.find('[role="tooltip"]').exists()).toBe(false);
    await move(center, 230);
    await svg.trigger('mouseleave');
    expect(wrapper.find('[role="tooltip"]').exists()).toBe(false);
  });
  test('hover retains unknown, zero and independent cache warning details', async () => {
    const wrapper = render();
    const svg = wrapper.get('svg');
    Object.defineProperty(svg.element, 'getBoundingClientRect', { value: () => ({ left: 0, top: 0, width: 760, height: 280 }) });
    await svg.trigger('mousemove', { clientX: 72 + 670 / 6, clientY: 240 });
    expect(wrapper.get('[role="tooltip"]').text()).toContain('--（未知）');
    await svg.trigger('mousemove', { clientX: 407, clientY: 240 });
    expect(wrapper.get('[role="tooltip"]').text()).toContain('0.0%');
    expect(wrapper.get('[role="tooltip"]').text()).toContain('缓存击穿：上下文 ≥ 5 万且缓存命中率低于 10%');
  });
  test('dense mouse hit testing selects the original point even when bars overlap', async () => {
    const data = Array.from({ length: 1500 }, (_, i) => ({ label: String(i), value: i }));
    const started = performance.now();
    const wrapper = render(data, 'tokens');
    const elapsed = performance.now() - started;
    console.info(`BarChart 1500 points happy-dom: ${elapsed.toFixed(0)}ms (regression reference)`);
    expect(elapsed).toBeLessThan(10000);
    expect(wrapper.findAll('[data-point]')).toHaveLength(1499);
    expect(wrapper.findAll('tbody tr')).toHaveLength(1500);
    const svg = wrapper.get('svg');
    Object.defineProperty(svg.element, 'getBoundingClientRect', { value: () => ({ left: 0, top: 0, width: 760, height: 280 }) });
    await svg.trigger('mousemove', { clientX: 72 + 670 * (987.5 / 1500), clientY: 240 });
    expect(wrapper.get('[role="tooltip"] strong').text()).toBe('987');
    expect(Number(wrapper.get('[data-point="987"] .bar').attributes('width'))).toBeGreaterThanOrEqual(1);
  });
});
