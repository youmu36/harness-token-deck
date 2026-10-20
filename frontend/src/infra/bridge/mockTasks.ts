import { AppError } from '../../app/errors';
import type { CommandData, EventMap, EventName, RequestMap } from '../../app/ports';
import { canonicalPayload } from '../../shared/payload';
type Task = CommandData<'tasks.get'>;
export function createMockTasks(emit: <K extends EventName>(name: K, payload: EventMap[K]) => void) {
  const tasks = new Map<string, Task>(); const timers = new Set<ReturnType<typeof setTimeout>>();
  const receipts = new Map<string, { fingerprint: string; result: CommandData<'models.scan'> | Task }>();
  const fail = (code: string): never => { throw new AppError({ code, message: code, retryable: false }); };
  function receipt(id: string, fingerprint: string) { const old = receipts.get(id); if (old && old.fingerprint !== fingerprint) fail('OPERATION_ID_REUSED'); return old?.result; }
  function get(taskId: string): Task { const task = tasks.get(taskId) ?? fail('TASK_NOT_FOUND'); return structuredClone(task); }
  function start(command: string, request: { operationId: string }, result: () => NonNullable<Task['result']>) {
    const fingerprint = canonicalPayload([command, request]); const old = receipt(request.operationId, fingerprint); if (old) return old as CommandData<'models.scan'>;
    const taskId = crypto.randomUUID(); const task: Task = { taskId, status: 'running', cancelRequested: false, completed: 0, total: 3, result: null, error: null };
    tasks.set(taskId, task); const snapshot = { taskId }; receipts.set(request.operationId, { fingerprint, result: snapshot });
    const timer = setTimeout(() => {
      timers.delete(timer);
      if (task.cancelRequested) task.status = 'cancelled';
      else {
        try { task.completed = task.total; task.result = result(); task.status = 'completed'; }
        catch (failure) { task.status = 'failed'; task.error = failure instanceof AppError ? failure.info : { code: 'INTERNAL_ERROR', message: '任务失败', retryable: false }; }
      }
      emit('task.progress', { taskId, completed: task.completed, total: task.total });
      emit('task.done', { taskId, ok: task.status === 'completed', result: task.result, error: task.error });
    }, 250);
    timers.add(timer); return snapshot;
  }
  function cancel(request: RequestMap['tasks.cancel'], command = 'tasks.cancel') {
    const fingerprint = canonicalPayload([command, request]); const old = receipt(request.operationId, fingerprint); if (old) return old as Task;
    const task = tasks.get(request.taskId) ?? fail('TASK_NOT_FOUND');
    if (task.status === 'running' || task.status === 'pending') task.cancelRequested = true;
    const snapshot = get(request.taskId); receipts.set(request.operationId, { fingerprint, result: snapshot }); return snapshot;
  }
  return { start, get, cancel, dispose: () => { timers.forEach(timer => clearTimeout(timer)); timers.clear(); } };
}
