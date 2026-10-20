<template><Dialog :open="open" :title="title" :pending="pending" @update:open="$emit('update:open', $event)"><p>{{ message }}</p><InlineAlert v-if="error" severity="danger">{{ error }}</InlineAlert><template #footer><Button variant="secondary" :disabled="pending" @click="$emit('update:open', false)">取消</Button><Button variant="danger" :pending="pending" @click="$emit('confirm')">{{ confirmLabel }}</Button></template></Dialog></template>
<script setup lang="ts">
import Dialog from './Dialog.vue'; import Button from './Button.vue'; import InlineAlert from './InlineAlert.vue';
withDefaults(defineProps<{ open: boolean; title: string; message: string; confirmLabel?: string; pending?: boolean; error?: string }>(), { confirmLabel: '确认', pending: false });
defineEmits<{ 'update:open': [value: boolean]; confirm: [] }>();
</script>
