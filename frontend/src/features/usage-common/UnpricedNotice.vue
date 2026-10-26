<script setup lang="ts">
import { computed, inject, ref } from 'vue';
import { BridgeKey, type CommandData } from '../../app/ports';
import { unwrap, asAppError } from '../../app/errors';
import { useShellPreferences } from '../../app/useShellPreferences';
import { useQuery } from '../../shared/query';
import InlineAlert from '../../ui/InlineAlert.vue';
import Button from '../../ui/Button.vue';
const props=defineProps<{ harness?: string; initialPreferences: CommandData<'preferences.get'> }>();
const bridge=inject(BridgeKey)!;
const notice=useQuery('unpriced.notice',async()=>unwrap(await bridge.call('pricing.unpricedNotice',{})),{events:['usage.changed',{name:'quality.changed',immediate:true}],coalesce:true,eventIntervalMs:5000});
const preferences=useShellPreferences(props.initialPreferences);
const pending=ref(false); const error=ref('');
const relevant=computed(()=>notice.data.value && notice.data.value.count>0 && (!props.harness||notice.data.value.harnesses.includes(props.harness)));
const closed=computed(()=>preferences.snapshot.value.preferences.unpricedNoticeDismissed===notice.data.value?.signature);
async function toggle() { if(!notice.data.value||pending.value)return;pending.value=true;error.value='';try{await preferences.save({unpricedNoticeDismissed:closed.value?'':notice.data.value.signature});}catch(failure){error.value=asAppError(failure).message;}finally{pending.value=false;} }
</script>
<template><InlineAlert v-if="relevant && !closed" severity="warning">发现 {{ notice.data.value?.count }} 条未定价用量，本地估算可能偏低。<a href="#/unpriced">前往未定价模型补价</a><Button :pending="pending" @click="toggle">关闭提醒</Button></InlineAlert><p v-else-if="relevant && closed" class="text-muted"><Button :pending="pending" @click="toggle">恢复未定价提醒</Button></p><p v-if="error" role="alert">{{ error }}</p></template>
