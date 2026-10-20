import { readonly, ref } from 'vue';
export type ToastKind = 'success' | 'info';
type ToastItem = { id: number; message: string; kind: ToastKind };
const state = ref<ToastItem[]>([]);
const timers = new Map<number, ReturnType<typeof setTimeout>>();
export const toasts = readonly(state);
let nextId = 1;
export function dismiss(id: number) {
  state.value = state.value.filter(toast => toast.id !== id);
  const timer = timers.get(id);
  if (timer !== undefined) clearTimeout(timer);
  timers.delete(id);
}
export function useToast() {
  function show(message: string, kind: ToastKind = 'info') {
    const id = nextId++;
    state.value.push({ id, message, kind });
    timers.set(id, setTimeout(() => dismiss(id), 4000));
    return id;
  }
  return {
    show,
    success: (message: string) => show(message, 'success'),
    info: (message: string) => show(message, 'info'),
    dismiss,
  };
}
