<script setup lang="ts">
import { ref } from 'vue';
import type { Rule } from './types';
import Dialog from '../../ui/Dialog.vue';
import Button from '../../ui/Button.vue';
const props = defineProps<{ rule: Rule }>();
const emit = defineEmits<{ close: []; done: [rule: Rule] }>();
const weekdays = ref([...props.rule.weekdays]); const hours = ref([...props.rule.hours]); const offset = ref(props.rule.offsetMinutes);
const offsets = Array.from({ length: 113 }, (_, i) => -840 + i * 15);
const offsetName = (value: number) => `UTC${value < 0 ? '-' : '+'}${String(Math.floor(Math.abs(value) / 60)).padStart(2, '0')}:${String(Math.abs(value) % 60).padStart(2, '0')}`;
function done() { if (weekdays.value.length && hours.value.length) emit('done', { ...props.rule, weekdays: [...weekdays.value].sort((a,b) => a-b) as Rule['weekdays'], hours: [...hours.value].sort((a,b) => a-b) as Rule['hours'], offsetMinutes: offset.value }); }
</script>
<template><Dialog :open="true" title="峰时与计费时区" @update:open="emit('close')"><div class="peak-fields"><label>计费时区<select v-model.number="offset" aria-label="计费时区"><option :value="-new Date().getTimezoneOffset()">当前本机时区（{{ offsetName(-new Date().getTimezoneOffset()) }}）</option><option v-for="value in offsets" :key="value" :value="value">{{ offsetName(value) }}</option></select></label><div class="quick"><Button @click="weekdays = [1,2,3,4,5]">周一至周五</Button><Button @click="weekdays = [1,2,3,4,5,6,7]">全周</Button></div><div class="weekdays"><label v-for="(name, i) in ['一','二','三','四','五','六','日']" :key="i"><input v-model="weekdays" type="checkbox" :value="i + 1" />周{{ name }}</label></div><div class="quick"><Button @click="hours = Array.from({length:24}, (_, i) => i)">全天</Button><Button @click="hours = []">清除时段</Button></div><div class="hours"><label v-for="hour in 24" :key="hour"><input v-model="hours" type="checkbox" :value="hour - 1" :aria-label="`${hour - 1} 时`" />{{ String(hour - 1).padStart(2, '0') }}:00</label></div></div><template #footer><Button @click="emit('close')">取消</Button><Button variant="primary" :disabled="!weekdays.length || !hours.length" @click="done">完成</Button></template></Dialog></template>
<style scoped>.peak-fields { display: grid; gap: 16px; }.peak-fields > label { display: grid; gap: 6px; }select { padding: 6px 8px; }.quick { display: flex; flex-wrap: wrap; gap: 8px; }.weekdays { display: flex; flex-wrap: wrap; gap: 10px; }.weekdays label, .hours label { display: flex; gap: 4px; align-items: center; font-size: 12px; }.hours { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 8px; }@media(max-width:480px) { .hours { grid-template-columns: repeat(4, minmax(0, 1fr)); } }</style>
