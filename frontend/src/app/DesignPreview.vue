<script setup lang="ts">
import { ref, watchEffect } from 'vue';

const theme = ref(new URLSearchParams(location.search).get('theme') === 'dark' ? 'dark' : 'light');
watchEffect(() => { document.documentElement.dataset.theme = theme.value; });
const statuses = [
  { token: 'ok', label: '采集正常' }, { token: 'warn', label: '用量不完整' },
  { token: 'danger', label: '连接失败' }, { token: 'info', label: '正在更新' },
];
const series = [
  { token: 'chart-1', label: '输入', value: '86,420' },
  { token: 'chart-2', label: '缓存', value: '61,280' },
  { token: 'chart-3', label: '输出', value: '12,560' },
  { token: 'chart-4', label: '成本', value: '$0.1524' },
];
</script>

<template>
  <main class="content preview">
    <header>
      <div><p class="text-muted">Harness Token Deck</p><h1>视觉基础</h1><p class="text-muted">颜色、排版与控件状态</p></div>
      <div class="themes" aria-label="预览主题">
        <button :aria-pressed="theme === 'light'" @click="theme = 'light'">浅色</button>
        <button :aria-pressed="theme === 'dark'" @click="theme = 'dark'">深色</button>
      </div>
    </header>
    <section class="metrics">
      <article class="surface"><h2 class="text-muted">总 Token</h2><p class="value">98,980</p><p class="text-faint">输入与输出合计</p></article>
      <article class="surface"><h2 class="text-muted">缓存命中率</h2><p class="value">70.9<span>%</span></p><p class="text-faint">已知缓存的输入比例</p></article>
      <article class="surface"><h2 class="text-muted">估算成本</h2><p class="value money">$0.1524</p><p class="text-faint">估算仅供参考</p></article>
    </section>
    <section class="details">
      <article class="surface panel">
        <h2>文字与输入</h2><p>安静、信息密度适中的本地账本。</p>
        <p class="text-muted">说明文字清晰可读，数字保持等宽。</p>
        <label>模型名称<input placeholder="输入模型名称" aria-label="模型名称" /></label>
        <div class="actions"><button class="primary">保存设置</button><button class="secondary">重新读取</button></div>
        <div class="secondary-surface"><p class="text-faint">占位和辅助文字在次级背景上保持清晰。</p><a href="#status">查看状态</a></div>
      </article>
      <article id="status" class="surface panel">
        <h2>状态与提示</h2>
        <div class="badges"><span v-for="status in statuses" :key="status.token" :style="{ color: `var(--${status.token})`, background: `var(--${status.token}-soft)` }">{{ status.label }}</span></div>
        <div class="notice"><strong>当前选择：Codex</strong><p>刷新保留当前数据，操作失败可重试。</p></div>
        <div class="warning"><strong>部分请求尚未定价</strong><p>完整金额以已入账的请求为准。</p></div>
      </article>
    </section>
    <section class="surface panel palette">
      <h2>图表与缓存警示</h2>
      <div class="series"><div v-for="item in series" :key="item.token"><span class="swatch" :style="{ background: `var(--${item.token})` }"></span><div><p>{{ item.label }}</p><p class="text-muted">{{ item.value }}</p></div></div></div>
      <div class="cache-flags"><span><i style="background: var(--cache-bust)"></i>缓存击穿</span><span><i style="background: var(--cache-loss)"></i>缓存大量下降</span></div>
    </section>
  </main>
</template>

<style scoped>
.preview { padding-block: 36px; max-width: 1240px; }
header, .actions, .badges, .series, .cache-flags { display: flex; align-items: center; gap: var(--space-3); }
header { justify-content: space-between; margin-bottom: var(--space-6); }
header h1 { margin-block: var(--space-2); }
button { padding: 7px 14px; border-radius: 6px; border: 1px solid var(--border); background: var(--surface); }
.themes { display: flex; gap: var(--space-1); }
.themes [aria-pressed='true'] { background: var(--accent-soft); border-color: var(--accent); color: var(--accent); }
.metrics { display: grid; grid-template-columns: repeat(3, 1fr); gap: var(--space-4); margin-bottom: var(--space-4); }
.metrics article { padding: var(--space-6); }
.metrics h2 { font-size: 14px; }
.value { font-size: 34px; line-height: 1.3; font-weight: 600; margin-block: var(--space-3); }
.value span { font-size: 20px; margin-left: 4px; }
.details { display: grid; grid-template-columns: 1fr 1fr; gap: var(--space-4); margin-bottom: var(--space-4); }
.panel { padding: var(--space-6); }
.panel h2 { margin-bottom: var(--space-4); }
.panel > p + p { margin-top: var(--space-1); }
label { display: grid; gap: var(--space-2); margin-block: var(--space-4); }
input { padding: 7px 10px; border-radius: 6px; }
.primary { background: var(--accent); color: var(--accent-contrast); border-color: var(--accent); }
.secondary-surface { background: var(--surface-2); padding: var(--space-3); border-radius: 6px; margin-top: var(--space-4); }
.secondary-surface a { display: inline-block; min-height: 28px; padding-top: 4px; }
.badges { flex-wrap: wrap; }
.badges span { padding: 4px 9px; border-radius: 4px; }
.notice, .warning { margin-top: var(--space-4); padding: var(--space-4); border-radius: 6px; }
.notice { color: var(--info); background: var(--info-soft); }
.warning { color: var(--warn); background: var(--warn-soft); }
.notice p, .warning p { margin-top: var(--space-2); }
.series { justify-content: space-between; }
.series > div { display: flex; align-items: center; gap: var(--space-3); flex: 1; }
.swatch { width: 44px; height: 44px; border-radius: 4px; }
.cache-flags { border-top: 1px solid var(--border); margin-top: var(--space-4); padding-top: var(--space-4); gap: var(--space-6); }
.cache-flags span { display: flex; align-items: center; gap: var(--space-2); }
.cache-flags i { width: 24px; height: 6px; display: inline-block; }
</style>
