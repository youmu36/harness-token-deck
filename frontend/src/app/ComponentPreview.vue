<script setup lang="ts">
import { ref, watchEffect } from 'vue';
import { RefreshCw } from 'lucide-vue-next';
import Badge from '../ui/Badge.vue';
import Button from '../ui/Button.vue';
import IconButton from '../ui/IconButton.vue';
import MetricCard from '../ui/MetricCard.vue';
import Money from '../ui/Money.vue';
import InlineAlert from '../ui/InlineAlert.vue';
import SegmentedControl from '../ui/SegmentedControl.vue';
import DateTimeField from '../ui/DateTimeField.vue';
import DataTable from '../ui/DataTable.vue';
import Dialog from '../ui/Dialog.vue';
import ConfirmDialog from '../ui/ConfirmDialog.vue';
import EmptyState from '../ui/EmptyState.vue';
import Skeleton from '../ui/Skeleton.vue';
import Toast from '../ui/Toast.vue';
import { useToast } from '../ui/useToast';
import { formatInteger, formatTokenCount } from '../shared/format';
import type { TimeZoneMode } from '../shared/time';
import BarChart from '../ui/charts/BarChart.vue';
import type { ChartPoint } from '../ui/charts/axis';

const theme = ref(new URLSearchParams(location.search).get('theme') === 'dark' ? 'dark' : 'light');
watchEffect(() => { document.documentElement.dataset.theme = theme.value; });
const period = ref('today');
const mode = ref<TimeZoneMode>('UTC');
const start = ref('2026-10-06 08:17');
const end = ref('2026-10-06 12:34');
const editing = ref(false); const confirming = ref(false); const error = ref(''); const draft = ref('5.00');
const toast = useToast();
const rows = Array.from({ length: 250 }, (_, i) => ({ id: i + 1, model: i % 2 ? 'GPT 6' : 'DeepSeek V4', tokens: (i + 1) * 1234 }));
const cachePoints: ChartPoint[] = Array.from({ length: 60 }, (_, i) => ({
  label: `10-06 12:${String(i).padStart(2, '0')}`, value: i === 14 ? null : i === 28 ? 0 : (40 + (i * 17) % 57) / 100,
  flags: { cacheBust: i === 28 || i === 42, cacheLoss: i === 37 || i === 42 },
}));
const trendPoints: ChartPoint[] = Array.from({ length: 24 }, (_, i) => ({
  label: `10-06 ${String(i).padStart(2, '0')}:00`, value: (i + 1) * 4500,
}));
</script>

<template>
  <main class="content preview">
    <header><div><p class="text-muted">Harness Token Deck</p><h1>通用组件</h1><p class="text-muted">控件、金额和日期时间</p></div>
      <SegmentedControl v-model="theme" label="预览主题" :options="[{ value: 'light', label: '浅色' }, { value: 'dark', label: '深色' }]" />
    </header>
    <section class="metrics">
      <MetricCard title="总 Token" :value="formatTokenCount(123456)" :full-value="formatInteger(123456)" description="输入与输出合计"><template #badge><Badge tone="success">采集正常</Badge></template></MetricCard>
      <MetricCard title="估算成本" money description="估算仅供参考"><template #value><Money pico="1234567890123456" /></template></MetricCard>
      <MetricCard title="供应商余额" money description="两位小数向零截断"><template #value><Money pico="-1239000000000" mode="trunc2" /></template></MetricCard>
    </section>
    <section class="panels">
      <article class="surface panel">
        <h2>操作与状态</h2>
        <div class="actions"><Button variant="primary" @click="toast.success('已保存')">保存</Button><Button pending>保存中</Button><Button variant="ghost" @click="toast.info('刷新完成')">刷新</Button><IconButton label="重新读取" @click="toast.info('已重新读取')"><RefreshCw :size="16" /></IconButton></div>
        <SegmentedControl v-model="period" label="时间范围" :options="[{ value: 'today', label: '今天' }, { value: '7days', label: '近 7 天' }, { value: 'custom', label: '自定义' }]" />
        <InlineAlert severity="warning">部分请求尚未定价</InlineAlert>
        <div class="badges"><Badge>本机账本</Badge><Badge tone="success">正常</Badge><Badge tone="warning">不完整</Badge><Badge tone="danger">需关注</Badge></div>
        <p class="text-muted">钱包完整金额 <Money pico="12345678000000" mode="full6" /></p>
        <div class="actions"><Button @click="editing = true">打开编辑对话框</Button><Button variant="danger" @click="confirming = true">打开确认对话框</Button></div>
      </article>
      <article class="surface panel">
        <h2>日期时间范围</h2>
        <SegmentedControl v-model="mode" label="时区" :options="[{ value: 'system', label: '系统时区' }, { value: 'UTC', label: 'UTC' }]" />
        <label for="start-time">开始</label><DateTimeField id="start-time" v-model="start" :mode="mode" label="开始" />
        <label for="end-time">结束</label><DateTimeField id="end-time" v-model="end" :mode="mode" label="结束" />
        <p class="text-muted">输入精确到分钟；在日历中使用方向键和翻页键选择。</p>
        <div class="skeleton-row"><Skeleton width="100px" height="24px" /><Skeleton width="160px" height="24px" /></div>
      </article>
    </section>
    <section class="surface panel">
      <h2>近期缓存监控</h2>
      <BarChart label="缓存命中率" :points="cachePoints" y-format="percent" :x-tick-count="4" show-warnings />
      <p class="text-muted">14 分未知，28 分为零；42 分同时出现两种缓存警示。</p>
    </section>
    <section class="surface panel chart-panel">
      <h2>用量趋势</h2><BarChart label="Token 趋势" :points="trendPoints" :x-tick-count="4" />
    </section>
    <section class="surface panel">
      <h2>用量明细</h2>
      <DataTable :rows="rows" :height="240" label="预览用量明细" :columns="[
        { key: 'model', label: '模型', sortable: true },
        { key: 'tokens', label: 'Token', sortable: true, numeric: true, format: value => formatInteger(Number(value)) },
      ]" />
    </section>
    <EmptyState message="当前范围暂无入账用量" />
  </main>
  <Dialog v-model:open="editing" title="充值" :error="error">
    <label for="amount">充值金额（USD）</label><input id="amount" v-model="draft" />
    <template #footer><Button @click="editing = false">取消</Button><Button variant="primary" @click="error = '保存失败，请检查输入后重试'">模拟保存失败</Button></template>
  </Dialog>
  <ConfirmDialog v-model:open="confirming" title="清理历史账本" message="清理后无法恢复历史用量，请确认所选范围。"
    @confirm="confirming = false; toast.success('预览确认完成')" />
  <Toast />
</template>

<style scoped>
.preview { max-width: 1280px; padding-block: 28px; }
header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
header h1 { margin-block: 6px; }
.metrics { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin-bottom: 16px; }
.panels { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px; }
.panel { padding: 20px; display: grid; align-content: start; gap: 14px; }
.chart-panel { margin-block: 16px; }
.actions, .badges, .skeleton-row { display: flex; gap: 8px; align-items: center; }
.panel label { margin-bottom: -8px; }
#amount { display: block; padding: 6px 8px; border-radius: 4px; margin-top: 8px; width: 100%; }
</style>
