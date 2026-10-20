<template><p v-if="show" class="usage-footnote text-muted">* 缓存覆盖不足，命中率仅按缓存已知记录加权；新增输入不可确定，显示 --。</p></template>
<script setup lang="ts">defineProps<{ show: boolean }>();</script>
<style scoped>.usage-footnote { font-size: 12px; margin-top: 10px; }</style>
