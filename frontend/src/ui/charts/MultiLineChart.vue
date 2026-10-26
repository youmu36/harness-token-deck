<script setup lang="ts">
import { computed, nextTick, ref, useId } from 'vue';
export interface LineSeries { key: string; name: string; points: { day: string; value: number | null; label: string }[] }
const props = defineProps<{ series: readonly LineSeries[]; label: string; unit?: string; axisUnit?:string; days?: readonly string[] }>();
const colors = ['var(--chart-1)', 'var(--chart-2)', 'var(--chart-3)', 'var(--chart-4)', 'var(--chart-5)'];
const days = computed(() => {
  if (props.days) return [...props.days];
  const known = [...new Set(props.series.flatMap(line => line.points.map(point => point.day)))].sort();
  if (!known.length || !known.every(day => /^\d{4}-\d{2}-\d{2}$/.test(day))) return known;
  const start = Date.parse(`${known[0]}T00:00:00Z`), end = Date.parse(`${known.at(-1)}T00:00:00Z`);
  if ((end - start) / 86400000 > 120) return known;
  return Array.from({length: Math.floor((end - start) / 86400000) + 1}, (_, index) => new Date(start + index * 86400000).toISOString().slice(0, 10));
});
const max = computed(() => Math.max(1, ...props.series.flatMap(line => line.points.flatMap(point => point.value === null || !Number.isFinite(point.value) ? [] : [point.value]))) * 1.12);
const x = (index: number) => 74 + index * 864 / Math.max(1, days.value.length - 1);
const y = (value: number) => 238 - Math.max(0, value) * 212 / max.value;
const lines = computed(() => props.series.slice(0, 5).map((line, index) => {
  const byDay = new Map(line.points.map(point => [point.day, point]));
  let path = '', connected = false, previousDay = '';
  const dots = days.value.flatMap((day, i) => {
    const point = byDay.get(day);
    if (!point || point.value === null || !Number.isFinite(point.value)) { connected = false; return []; }
    if (/^\d{4}-\d{2}-\d{2}$/.test(day) && previousDay && Date.parse(day) - Date.parse(previousDay) > 86400000) connected = false;
    previousDay = day;
    path += `${connected ? 'L' : 'M'}${x(i)},${y(point.value)} `; connected = true;
    return [{ ...point, x: x(i), y: y(point.value) }];
  });
  return { ...line, color: colors[index], path, dots, byDay };
}));
const chart = ref<SVGSVGElement>();
const tooltipId = useId(), hoveredDay = ref<string>(), keyboardDay = ref<string>();
const tooltipDay = computed(() => hoveredDay.value ?? keyboardDay.value);
const tooltipRows = computed(() => !tooltipDay.value ? [] : lines.value.map(line => ({key:line.key,name:line.name,color:line.color,label:line.byDay.get(tooltipDay.value!)?.label ?? '—'})));
const tooltipX = computed(() => `${x(Math.max(0,days.value.indexOf(tooltipDay.value ?? ''))) / 980 * 100}%`);
function hover(event: MouseEvent) {
  const rect = chart.value?.getBoundingClientRect();
  if (!rect?.width || !rect.height) return;
  const scale = Math.min(rect.width / 980, rect.height / 282);
  const px = (event.clientX - rect.left - (rect.width - 980 * scale) / 2) / scale;
  const py = (event.clientY - rect.top - (rect.height - 282 * scale) / 2) / scale;
  hoveredDay.value = px < 74 || px > 938 || py < 26 || py > 238 ? undefined : days.value[Math.min(days.value.length-1,Math.round((px-74) * Math.max(1,days.value.length-1) / 864))];
}
const tableOpen = ref(false);
function toggleTable(event: Event) { tableOpen.value = (event.target as HTMLDetailsElement).open; }
const focused = ref('');
const pointKeys = computed(() => lines.value.flatMap(line => line.dots.map(dot => `${line.key}:${dot.day}`)));
const firstPoint = computed(() => pointKeys.value[0]);
const activePoint = computed(() => pointKeys.value.includes(focused.value) ? focused.value : firstPoint.value);
async function move(event: KeyboardEvent, lineIndex: number, dotIndex: number) {
  if (event.key === 'Escape') { hoveredDay.value = keyboardDay.value = undefined; return; }
  if (!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Home','End'].includes(event.key)) return;
  event.preventDefault();
  const line = event.key === 'ArrowUp' ? Math.max(0, lineIndex - 1) : event.key === 'ArrowDown' ? Math.min(lines.value.length - 1, lineIndex + 1) : lineIndex;
  const count = lines.value[line]!.dots.length;
  if (!count) return;
  const index = event.key === 'Home' ? 0 : event.key === 'End' ? count - 1 : Math.max(0, Math.min(count - 1, dotIndex + (event.key === 'ArrowLeft' ? -1 : event.key === 'ArrowRight' ? 1 : 0)));
  focused.value = `${lines.value[line]!.key}:${lines.value[line]!.dots[index]!.day}`;
  await nextTick(); chart.value?.querySelector<SVGElement>(`circle[data-line="${line}"][data-point="${index}"]`)?.focus();
}
</script>

<template>
  <figure class="multi-line" :aria-label="label">
    <ul class="line-legend"><li v-for="line in lines" :key="line.key"><i :style="{ background: line.color }" />{{ line.name }}</li><li v-if="axisUnit !== undefined && unit" class="line-unit">单位：{{unit.trim()}}</li></ul>
    <p v-if="!firstPoint" class="text-muted">当前范围暂无可用样本</p>
    <div v-else class="plot" @mouseleave="hoveredDay=undefined">
    <svg ref="chart" viewBox="0 0 980 282" role="img" :aria-label="label" @mousemove="hover">
      <g v-for="i in 5" :key="i" class="axis"><line x1="74" x2="938" :y1="y(max * (i - 1) / 4)" :y2="y(max * (i - 1) / 4)" /><text x="64" :y="y(max * (i - 1) / 4) + 4" text-anchor="end">{{ (max * (i - 1) / 4).toLocaleString('en-US', { maximumFractionDigits: 1, notation: 'compact' }) }}{{ axisUnit ?? unit }}</text></g>
      <line v-if="tooltipDay && days.includes(tooltipDay)" class="crosshair" :x1="x(days.indexOf(tooltipDay))" :x2="x(days.indexOf(tooltipDay))" y1="26" y2="238" />
      <template v-for="(line, lineIndex) in lines" :key="line.key"><path :d="line.path" :stroke="line.color" fill="none" stroke-width="2.6" vector-effect="non-scaling-stroke" /><circle v-for="(dot, dotIndex) in line.dots" :key="dot.day" :data-line="lineIndex" :data-point="dotIndex" :cx="dot.x" :cy="dot.y" :r="dot.day === tooltipDay ? 6 : 5" :fill="line.color" :tabindex="`${line.key}:${dot.day}` === activePoint ? 0 : -1" :aria-describedby="dot.day === tooltipDay ? tooltipId : undefined" :aria-label="`${line.name} ${dot.day} ${dot.label}`" @focus="focused=`${line.key}:${dot.day}`;keyboardDay=dot.day" @blur="keyboardDay=undefined" @keydown="move($event, lineIndex, dotIndex)" /></template>
      <g class="axis"><text v-for="i in [...new Set([0, Math.floor((days.length - 1) / 2), days.length - 1])]" :key="i" :x="x(i)" y="268" text-anchor="middle">{{ days[i] }}</text></g>
    </svg>
    <div v-if="tooltipDay && days.includes(tooltipDay)" :id="tooltipId" role="tooltip" class="line-tooltip" :style="{'--tooltip-x':tooltipX}"><strong>{{tooltipDay}}</strong><article v-for="row in tooltipRows" :key="row.key" :style="{borderColor:row.color}"><span>{{row.name}}</span><b>{{row.label}}</b></article></div>
    </div>
    <details v-if="days.length" @toggle="toggleTable"><summary>查看图表数据</summary><div v-if="tableOpen" class="chart-table"><table><thead><tr><th>日期</th><th v-for="line in lines" :key="line.key">{{ line.name }}</th></tr></thead><tbody><tr v-for="day in days" :key="day"><td>{{ day }}</td><td v-for="line in lines" :key="line.key">{{ line.points.find(point => point.day === day)?.label ?? '—' }}</td></tr></tbody></table></div></details>
  </figure>
</template>

<style scoped>
.multi-line{margin:0;min-width:0}.line-legend{display:flex;flex-wrap:wrap;gap:10px 22px;list-style:none;padding:0;font-size:13px}.line-legend li{display:flex;align-items:center;gap:7px}.line-legend i{width:18px;height:3px;border-radius:2px;flex-shrink:0}svg{width:100%;height:auto;max-height:350px;overflow:visible}.axis line{stroke:var(--border);opacity:.4}.axis text{fill:var(--text-muted);font-size:12px}circle:focus{outline:2px solid var(--accent);outline-offset:4px}summary{font-size:12px;color:var(--text-muted);cursor:pointer}.chart-table{overflow:auto;max-height:300px}table{font-size:12px;border-collapse:collapse}td,th{padding:7px 12px;border-bottom:1px solid var(--border);text-align:left;white-space:nowrap}
.plot{position:relative}.line-unit{color:var(--text-muted);font-size:12px}.crosshair{stroke:var(--text-muted);stroke-dasharray:4 4;pointer-events:none}.line-tooltip{position:absolute;top:10px;left:clamp(0px,var(--tooltip-x),max(0px,calc(100% - 280px)));width:min(280px,100%);box-sizing:border-box;padding:10px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);box-shadow:0 6px 20px #0003;z-index:4;pointer-events:none;font-size:12px;display:grid;gap:8px;overflow-wrap:anywhere}.line-tooltip article{border-left:3px solid;padding-left:8px;display:grid;gap:3px}.line-tooltip span{color:var(--text-muted)}.line-tooltip b{font-weight:500}
</style>
