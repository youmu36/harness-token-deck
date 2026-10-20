import { computed, inject, onScopeDispose, ref } from 'vue';
import { BridgeKey } from '../app/ports';
import { unwrap } from '../app/errors';
import { useCommand } from './command';
import { useQuery } from './query';

export function useTask() {
  const bridge = inject(BridgeKey)!;
  const taskId = ref('');
  const task = useQuery('task', () => bridge.call('tasks.get', { taskId: taskId.value }).then(unwrap), {
    deps: [taskId], visible: () => !!taskId.value,
    events: [
      { name: 'task.progress', when: event => event.taskId === taskId.value },
      { name: 'task.done', when: event => event.taskId === taskId.value },
    ], clearOnChange: true,
  });
  const cancel = useCommand('tasks.cancel');
  const active = computed(() => !!taskId.value && !task.error.value && (!task.data.value || ['pending', 'running'].includes(task.data.value.status)));
  const timer = setInterval(() => {
    if (active.value && bridge.events.isVisible() && !task.loading.value && !task.refreshing.value) void task.refresh();
  }, 1000);
  const unsubscribe = bridge.events.onVisibility(visible => {
    if (visible && active.value && !task.loading.value && !task.refreshing.value) void task.refresh();
  });
  onScopeDispose(() => { clearInterval(timer); unsubscribe(); });
  return { taskId, task, active, cancel: () => cancel.run({ taskId: taskId.value }).then(() => task.refresh()), cancelling: cancel.pending, cancelError: cancel.error };
}
