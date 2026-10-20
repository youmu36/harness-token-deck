<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, useId, watch } from 'vue';
import ChartTooltip from './ChartTooltip.vue';
import { createAxis, formatTick, formatValue, tickIndices, warningLabels, type AxisFormat, type ChartPoint } from './axis';

const props = withDefaults(defineProps<{
  points: readonly ChartPoint[];
  label?: string;
  yFormat?: AxisFormat;
  xTickCount?: number;
  height?: number;
  showWarnings?: boolean;
  warningGrain?: 'minute' | 'hour';
  emptyMessage?: string;
  variant?: 'bar' | 'line';
}>(), { label: '用量图表', yFormat: 'tokens', xTickCount: 6, height: 214, warningGrain: 'minute', emptyMessage: '当前范围暂无数据' });
const emit = defineEmits<{ select: [index: number] }>();
const id = useId();
const container = ref<HTMLElement>();
const svg = ref<SVGSVGElement>();
const width = ref(760);
const focusIndex = ref(0);
const focused = ref<number | null>(null);
const hovered = ref<number | null>(null);
let observer: ResizeObserver | undefined;
onMounted(() => {
  if (typeof ResizeObserver === 'undefined' || !container.value) return;
  observer = new ResizeObserver(entries => { width.value = Math.max(320, entries[0]?.contentRect.width ?? 760); });
  observer.observe(container.value);
});
onBeforeUnmount(() => observer?.disconnect());
watch(() => props.points, () => {
  focusIndex.value = Math.min(focusIndex.value, Math.max(0, props.points.length - 1));
  hovered.value = null;
  if (focused.value !== null) focused.value = props.points.length ? focusIndex.value : null;
});
const axis = computed(() => createAxis(props.points, props.yFormat));
const bottom = computed(() => props.height - 34);
const plotWidth = computed(() => width.value - 90);
const step = computed(() => plotWidth.value / Math.max(1, props.points.length));
const barWidth = computed(() => Math.max(props.points.length > 400 ? 1 : 2, step.value * 0.72));
const y = (value: number) => 8 + (axis.value.max - value) / (axis.value.max - axis.value.min) * (bottom.value - 8);
const x = (index: number) => 72 + step.value * (index + 0.5);
const xTicks = computed(() => tickIndices(props.points.length, props.xTickCount));
const line = computed(() => { let connected = false; return props.points.map((point, index) => { if (point.value === null) { connected = false; return ''; } const command = connected ? 'L' : 'M'; connected = true; return `${command}${x(index)},${y(point.value)}`; }).join(' '); });
const activeIndex = computed(() => hovered.value ?? focused.value);
const activePoint = computed(() => activeIndex.value === null ? undefined : props.points[activeIndex.value]);
const tooltipLeft = computed(() => Math.max(0, Math.min(100 - 240 / width.value * 100, x(activeIndex.value ?? 0) / width.value * 100)));
const series = computed(() => [...new Set(props.points.flatMap(point => point.segments?.map(s => s.label) ?? []))]);
const bars = computed(() => props.points.map(point => {
  let positive = 0; let negative = 0;
  const segments = point.segments?.length ? point.segments : [{ label: '数值', value: point.value }];
  return segments.flatMap((segment, index) => {
    if (point.value === null || segment.value === null) return [];
    const start = segment.value < 0 ? negative : positive;
    const end = start + segment.value;
    if (segment.value < 0) negative = end; else positive = end;
    const colorIndex = series.value.length ? series.value.indexOf(segment.label) : index;
    const color = segment.color ?? (series.value.length ? `var(--chart-${colorIndex % 4 + 1})` : `url(#${id}-bar-gradient)`);
    return [{ label: segment.label, y: Math.min(y(start), y(end)), height: Math.abs(y(end) - y(start)), color }];
  });
}));
function pointText(point: ChartPoint) {
  return `${point.tooltipLabel ?? point.label}，${point.value === null ? '未知' : point.valueLabel ?? formatValue(point.value, props.yFormat)}${warningLabels(point).map(label => `，${label}`).join('')}`;
}
async function keydown(event: KeyboardEvent, index: number) {
  if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); emit('select', index); return; }
  const targets: Record<string, number> = { ArrowRight: index + 1, ArrowDown: index + 1, ArrowLeft: index - 1, ArrowUp: index - 1, Home: 0, End: props.points.length - 1 };
  if (event.key === 'Escape') { hovered.value = null; focused.value = null; return; }
  const target = targets[event.key];
  if (target === undefined) return;
  event.preventDefault();
  focusIndex.value = Math.max(0, Math.min(props.points.length - 1, target));
  await nextTick();
  svg.value?.querySelector<SVGGElement>(`[data-point="${focusIndex.value}"]`)?.focus();
}
function mousemove(event: MouseEvent) {
  if (!svg.value || !props.points.length) return;
  const bounds = svg.value.getBoundingClientRect();
  if (!bounds.width || !bounds.height) { hovered.value = null; return; }
  const localX = (event.clientX - bounds.left) * width.value / bounds.width;
  const localY = (event.clientY - bounds.top) * props.height / bounds.height;
  if (localX < 72 || localX > width.value - 18 || localY < 8 || localY > bottom.value) {
    hovered.value = null; return;
  }
  const index = Math.min(props.points.length - 1, Math.floor((localX - 72) / step.value));
  const point = props.points[index]!;
  // Dense charts retain column-based lookup; sparse charts only respond over a visible bar.
  const halfWidth = props.points.length > 400 ? step.value / 2 : barWidth.value / 2;
  const overColumn = Math.abs(localX - x(index)) <= halfWidth;
  const overWarning = ((point.flags?.cacheBust || point.flags?.contextCompacted) && localY >= bottom.value - 10)
    || (point.flags?.cacheLoss && localY >= bottom.value - (point.flags?.cacheBust ? 18 : 8) && localY <= bottom.value - (point.flags?.cacheBust ? 10 : 0));
  const overBar = point.value === null
    ? localY >= y(0) - 18 && localY <= y(0)
    : props.variant === 'line' ? Math.abs(localY - y(point.value)) <= 12 : bars.value[index]!.some(bar => localY >= bar.y - 2 && localY <= bar.y + Math.max(2, bar.height));
  hovered.value = overColumn && (overBar || overWarning) ? index : null;
}
</script>

<template>
  <figure ref="container" class="bar-chart" :aria-labelledby="`${id}-title`">
    <figcaption :id="`${id}-title`" class="sr-only">{{ label }}</figcaption>
    <div v-if="points.length || emptyMessage" class="plot">
      <svg ref="svg" :viewBox="`0 0 ${width} ${height}`" :style="{ height: `${height}px` }" role="group" :aria-label="`${label}，方向键切换${variant === 'line' ? '数据点' : '柱子'}`" @mousemove="mousemove" @mouseleave="hovered = null">
        <defs>
          <linearGradient :id="`${id}-bar-gradient`" x1="0%" y1="100%" x2="0%" y2="0%">
            <stop offset="0%" class="bar-stop-bottom" />
            <stop offset="58%" class="bar-stop-mid" />
            <stop offset="100%" class="bar-stop-top" />
          </linearGradient>
        <linearGradient :id="`${id}-bust`" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#ff6680" /><stop offset="55%" stop-color="#d31342" /><stop offset="100%" stop-color="#9a0b32" /></linearGradient>
          <linearGradient :id="`${id}-loss`" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#b8325c" /><stop offset="60%" stop-color="#7d0f38" /><stop offset="100%" stop-color="#4d0621" /></linearGradient>
          <linearGradient :id="`${id}-sheen`" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="var(--chart-bar-highlight)" stop-opacity=".66" /><stop offset="100%" stop-color="var(--chart-bar-highlight)" stop-opacity="0" /></linearGradient>
        </defs>
        <rect class="plot-background" x="72" y="0" :width="plotWidth" :height="bottom" rx="12" aria-hidden="true" />
        <g aria-hidden="true" class="axis">
          <g v-for="tick in axis.ticks" :key="tick">
            <line x1="72" :x2="width - 18" :y1="y(tick)" :y2="y(tick)" />
            <text x="62" :y="y(tick) + 4" text-anchor="end">{{ formatTick(tick, yFormat) }}</text>
          </g>
          <text v-for="index in xTicks" :key="index" :x="x(index)" :y="height - 8" :text-anchor="index === 0 ? 'start' : index === points.length - 1 ? 'end' : 'middle'">{{ points[index]?.label }}</text>
        </g>
        <path v-if="variant === 'line'" :d="line" fill="none" stroke="var(--chart-1)" stroke-width="2" aria-hidden="true" class="line-series" />
        <g v-for="(point, index) in points" :key="index" :data-point="index" class="point" role="graphics-symbol" :tabindex="index === focusIndex ? 0 : -1"
          :aria-label="pointText(point)" :aria-describedby="activeIndex === index ? `${id}-tooltip` : undefined"
          @click="emit('select', index)" @focus="focusIndex = index; focused = index; hovered = null" @blur="focused = null" @keydown="keydown($event, index)">
          <rect class="hit-area" :x="x(index) - Math.max(barWidth, step) / 2" y="12" :width="Math.max(barWidth, step)" :height="bottom - 12" fill="transparent" />
          <rect v-if="point.value === null" class="unknown" :x="x(index) - barWidth / 2" :y="y(0) - 18" :width="barWidth" height="18" />
          <circle v-if="variant === 'line' && point.value !== null" :cx="x(index)" :cy="y(point.value)" r="4" fill="var(--chart-1)" />
          <foreignObject v-for="(bar, segment) in variant === 'line' ? [] : bars[index]" :key="segment" class="bar" :x="x(index) - barWidth / 2" :y="bar.y" :width="barWidth" :height="bar.height" aria-hidden="true">
            <div xmlns="http://www.w3.org/1999/xhtml" class="trend-fill" :style="point.segments?.length ? { background: bar.color } : undefined" />
          </foreignObject>
          <slot name="warnings" :point="point" :x="x(index) - barWidth / 2" :width="barWidth" :y="bottom - 10">
            <rect v-if="point.flags?.cacheBust || point.flags?.contextCompacted" class="cache-bust" rx="2" :fill="`url(#${id}-bust)`" :x="x(index) - barWidth / 2" :y="bottom - 10" :width="barWidth" height="10" />
            <rect v-if="point.flags?.cacheLoss && !point.flags?.cacheBust && !point.flags?.contextCompacted" class="cache-loss" rx="2" :fill="`url(#${id}-loss)`" :x="x(index) - barWidth / 2" :y="bottom - 8" :width="barWidth" height="8" />
          </slot>
        </g>
      </svg>
      <ChartTooltip v-if="activePoint" :id="`${id}-tooltip`" :point="activePoint" :y-format="yFormat" :left="tooltipLeft" :warning-grain="warningGrain" />
      <p v-if="!points.length && emptyMessage" class="empty">{{ emptyMessage }}</p>
    </div>
    <div v-if="series.length" class="legend">
      <span v-for="(name, index) in series" :key="name"><i :style="{ background: points.flatMap(p => p.segments ?? []).find(s => s.label === name)?.color ?? `var(--chart-${index % 4 + 1})` }" />{{ name }}</span>
    </div>
    <div class="sr-only"><table>
      <caption>{{ label }}数据表</caption>
      <thead><tr><th scope="col">时间</th><th scope="col">数值</th><th v-for="name in series" :key="name" scope="col">{{ name }}</th><th scope="col">警示</th></tr></thead>
      <tbody><tr v-for="(point, index) in points" :key="index">
        <th scope="row">{{ point.tooltipLabel ?? point.label }}</th><td>{{ point.value === null ? '--（未知）' : point.valueLabel ?? formatValue(point.value, yFormat) }}</td>
        <td v-for="name in series" :key="name">{{ formatValue(point.segments?.find(s => s.label === name)?.value ?? null, yFormat) }}</td>
        <td>{{ warningLabels(point).join('、') || '无' }}</td>
      </tr></tbody>
    </table></div>
  </figure>
</template>

<style scoped>
.bar-chart { position: relative; margin: 0; min-width: 0; }
.plot { position: relative; }
svg { width: 100%; overflow: visible; }
.axis { fill: var(--text-muted); font-size: 11px; }
.axis line { stroke: var(--border); stroke-width: 1; }
.bar-stop-bottom { stop-color: var(--chart-bar-bottom); }
.bar-stop-mid { stop-color: var(--chart-bar-mid); }
.bar-stop-top { stop-color: var(--chart-bar-top); }
.plot-background { fill: var(--chart-plot-bg); stroke: var(--border); stroke-width: 1; }
.bar { overflow: visible; pointer-events: none; }
.bar-highlight { opacity: .88; filter: drop-shadow(0 0 5px var(--chart-glow)); pointer-events: none; }
.unknown { fill: none; stroke: var(--text-muted); stroke-width: 1.5; stroke-dasharray: 3 2; }
.point:focus { outline: none; }
.point:focus-visible .hit-area { stroke: var(--accent); stroke-width: 2; }
.cache-bust { filter: drop-shadow(0 -1px 5px #ff3f68cc) drop-shadow(0 0 7px #d3134288); }
.cache-loss { filter: drop-shadow(0 -1px 4px #a3134188) drop-shadow(0 0 6px #6d0b2c66); }
.legend { display: flex; flex-wrap: wrap; gap: 8px 18px; font-size: 12px; color: var(--text-muted); margin-top: 8px; }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.legend i { display: inline-block; width: 16px; height: 4px; flex-shrink: 0; }
.legend .cache-loss { background: repeating-linear-gradient(90deg, var(--cache-loss) 0 3px, transparent 3px 5px); }
.empty { position: absolute; inset: 40% 0 auto; text-align: center; color: var(--text-muted); }
</style>

<style>
.bar-chart .trend-fill {
  width: 100%;
  height: 100%;
  position: relative;
  overflow: hidden;
  background: linear-gradient(180deg, var(--chart-bar-top) 0%, var(--chart-bar-mid) 42%, var(--chart-bar-bottom) 100%);
  box-shadow: var(--chart-bar-shadow);
  border-radius: 2px 2px 0 0;
  min-height: 0;
}
html:not([data-theme="dark"]) .bar-chart .trend-fill {
  background: linear-gradient(180deg, var(--chart-bar-top), var(--chart-bar-mid) 55%, var(--chart-bar-bottom));
  box-shadow: var(--chart-bar-shadow);
  border: 1px solid var(--chart-bar-edge);
  border-bottom: 0;
}
.bar-chart .trend-fill::before {
  content: "";
  position: absolute;
  left: 18%;
  right: 18%;
  top: -3px;
  height: 10px;
  background: radial-gradient(ellipse at center, var(--chart-bar-aura), transparent 72%);
  filter: blur(2px);
  pointer-events: none;
}
.bar-chart .trend-fill::after {
  content: "";
  position: absolute;
  left: 0;
  right: 0;
  top: 0;
  height: 34%;
  background: linear-gradient(180deg, var(--chart-bar-highlight), transparent);
  pointer-events: none;
}
html:not([data-theme="dark"]) .bar-chart .trend-fill::before,
html:not([data-theme="dark"]) .bar-chart .trend-fill::after { display: none; }
html[data-theme="dark"] .bar-chart .trend-fill::before {
  display: none;
}
</style>
