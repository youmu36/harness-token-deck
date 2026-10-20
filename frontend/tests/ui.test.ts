import { defineComponent, h, nextTick, ref } from 'vue';
import { mount } from '@vue/test-utils';
import { afterEach, describe, expect, it, vi } from 'vitest';
import Button from '../src/ui/Button.vue';
import IconButton from '../src/ui/IconButton.vue';
import SegmentedControl from '../src/ui/SegmentedControl.vue';
import DataTable from '../src/ui/DataTable.vue';
import Dialog from '../src/ui/Dialog.vue';
import ConfirmDialog from '../src/ui/ConfirmDialog.vue';
import Toast from '../src/ui/Toast.vue';
import { toasts } from '../src/ui/toastState';
import { useToast } from '../src/ui/useToast';

const cleanup: (() => void)[] = [];
afterEach(() => { cleanup.splice(0).forEach(fn => fn()); vi.useRealTimers(); document.body.innerHTML = ''; });
function fixture(component: Parameters<typeof mount>[0], options: Parameters<typeof mount>[1] = {}) {
  const wrapper = mount(component, { attachTo: document.body, ...options });
  cleanup.push(() => wrapper.unmount());
  return wrapper;
}
it('pending disables only the clicked button; icon buttons expose their label', async () => {
  const wrapper = fixture(Button, { props: { pending: true }, slots: { default: '保存' } });
  expect(wrapper.get('button').attributes('disabled')).toBeDefined();
  expect(wrapper.get('button').attributes('aria-busy')).toBe('true');
  await wrapper.get('button').trigger('click');
  expect(wrapper.emitted('click')).toBeUndefined();
  const icon = fixture(IconButton, { props: { label: '刷新' } });
  expect(icon.get('button').attributes()).toMatchObject({ 'aria-label': '刷新', title: '刷新' });
});
it('radio arrows wrap, skip disabled options, and select without a mouse', async () => {
  const value = ref('today');
  const host = defineComponent({ setup: () => () => h(SegmentedControl, {
    modelValue: value.value, 'onUpdate:modelValue': (next: string | number) => { value.value = String(next); },
    label: '时间范围', options: [{ value: 'today', label: '今天' }, { value: 'custom', label: '自定义', disabled: true }, { value: '7days', label: '近 7 天' }],
  }) });
  const wrapper = fixture(host);
  const today = wrapper.get('[data-value="today"]');
  (today.element as HTMLElement).focus();
  await today.trigger('keydown', { key: 'ArrowRight' });
  expect(value.value).toBe('7days');
  expect(document.activeElement?.getAttribute('aria-checked')).toBe('true');
  await wrapper.get('[data-value="7days"]').trigger('keydown', { key: 'ArrowDown' });
  expect(value.value).toBe('today');
  await today.trigger('keydown', { key: 'End' });
  expect(value.value).toBe('7days');
});
describe('DataTable', () => {
  it('keeps scroll through background snapshots and resets only for explicit filters', async () => {
    const rows = Array.from({ length: 500 }, (_, id) => ({ id, value: id }));
    const wrapper = mount(DataTable, { attachTo: document.body, props: { rows, columns: [{ key: 'value', label: '值' }], resetKey: 'all' } });
    cleanup.push(() => wrapper.unmount());
    const scroller = wrapper.get('.table-wrap').element as HTMLElement;
    scroller.scrollTop = 8000; scroller.scrollLeft = 80;
    await wrapper.get('.table-wrap').trigger('scroll');
    await wrapper.setProps({ rows: rows.map(row => ({ ...row, value: row.value + 1 })) });
    expect(scroller.scrollTop).toBe(8000); expect(scroller.scrollLeft).toBe(80);
    await wrapper.setProps({ resetKey: 'model-a' });
    expect(scroller.scrollTop).toBe(0);
  });
  it('sorts numbers, declares aria-sort, and passes the actual row to formatters', async () => {
    const wrapper = fixture(DataTable, { props: {
      rows: [{ id: 'a', value: 10 }, { id: 'b', value: 2 }],
      columns: [{ key: 'value', label: '用量', sortable: true, numeric: true, format: (value: unknown, row: Record<string, unknown>) => row.id + ':' + value }],
    } });
    expect(wrapper.get('th').attributes('aria-sort')).toBe('none');
    await wrapper.get('th button').trigger('click');
    expect(wrapper.get('th').attributes('aria-sort')).toBe('ascending');
    expect(wrapper.findAll('tbody tr').map(row => row.text())).toEqual(['b:2', 'a:10']);
    await wrapper.get('th button').trigger('click');
    expect(wrapper.get('th').attributes('aria-sort')).toBe('descending');
    expect(wrapper.findAll('tbody tr')[0]!.text()).toBe('a:10');
  });
  it('virtualizes more than 200 rows and reaches the end instead of truncating', async () => {
    const wrapper = fixture(DataTable, { props: {
      rows: Array.from({ length: 1000 }, (_, i) => ({ id: i, value: i })),
      columns: [{ key: 'value', label: '用量', sortable: true }], height: 400,
    } });
    expect(wrapper.findAll('tbody tr:not(.spacer)').length).toBeLessThan(25);
    const scroller = wrapper.get('.table-wrap');
    (scroller.element as HTMLElement).scrollTop = 1000 * 40 - 400;
    await scroller.trigger('scroll');
    expect(wrapper.text()).toContain('999');
    expect(wrapper.get('table').attributes('aria-rowcount')).toBe('1001');
    await wrapper.get('th button').trigger('click');
    expect((scroller.element as HTMLElement).scrollTop).toBe(0);
  });
});
describe('native Dialog', () => {
  it('returns focus without scrolling and never refocuses a closed dialog on background unmount', async () => {
    const opener = document.createElement('button'); document.body.append(opener); opener.focus();
    const focus = vi.spyOn(opener, 'focus');
    const wrapper = mount(Dialog, { attachTo: document.body, props: { open: true, title: '提示' } });
    cleanup.push(() => wrapper.unmount());
    await nextTick(); await nextTick();
    await wrapper.setProps({ open: false });
    document.querySelector('dialog')!.dispatchEvent(new Event('close'));
    expect(focus).toHaveBeenCalledWith({ preventScroll: true });
    const calls = focus.mock.calls.length;
    wrapper.unmount();
    expect(focus.mock.calls).toHaveLength(calls);
  });
  it('focuses the draft, retains inline failures, blocks pending closure and returns focus', async () => {
    const opener = document.createElement('button'); opener.textContent = '充值'; document.body.append(opener); opener.focus();
    const opened = ref(false); const pending = ref(false);
    const host = defineComponent({ setup: () => () => h(Dialog, {
      open: opened.value, 'onUpdate:open': (next: boolean) => { opened.value = next; }, title: '充值', pending: pending.value, error: '保存失败',
    }, { default: () => h('input', { value: '5.00' }) }) });
    const wrapper = fixture(host);
    opened.value = true; await nextTick(); await nextTick();
    const dialog = document.querySelector('dialog')!;
    expect(dialog.open).toBe(true);
    expect(document.activeElement?.tagName).toBe('INPUT');
    dialog.querySelector('input')!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true }));
    expect(document.activeElement).toBe(dialog.querySelector('button'));
    dialog.querySelector('button')!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true, cancelable: true }));
    expect(document.activeElement).toBe(dialog.querySelector('input'));
    expect(dialog.querySelector('[role="alert"]')?.textContent).toContain('保存失败');
    expect(dialog.querySelector('input')?.value).toBe('5.00');
    pending.value = true; await nextTick();
    const cancel = new Event('cancel', { cancelable: true });
    dialog.dispatchEvent(cancel); await nextTick();
    expect(cancel.defaultPrevented).toBe(true);
    expect(opened.value).toBe(true);
    expect(dialog.querySelector('button')?.disabled).toBe(true);
    pending.value = false; await nextTick();
    dialog.dispatchEvent(new Event('cancel', { cancelable: true })); await nextTick(); await nextTick();
    expect(dialog.open).toBe(false);
    expect(document.activeElement).toBe(opener);
    wrapper.unmount(); cleanup.pop();
  });
  it('ConfirmDialog states consequences and emits confirmation once', async () => {
    const wrapper = fixture(ConfirmDialog, { props: { open: true, title: '清理', message: '清理后无法恢复。', error: '清理失败' } });
    await nextTick(); await nextTick();
    const dialog = document.querySelector('dialog')!;
    expect(dialog.textContent).toContain('清理后无法恢复。');
    expect(dialog.querySelector('[role="alert"]')?.textContent).toContain('清理失败');
    const danger = dialog.querySelector<HTMLButtonElement>('.button--danger')!;
    danger.click(); await nextTick();
    expect(wrapper.emitted('confirm')).toHaveLength(1);
  });
});
it('success/info toasts expire after four seconds and can be dismissed', async () => {
  vi.useFakeTimers();
  const wrapper = fixture(Toast); const toast = useToast();
  const { success } = toast;
  const id = success('已保存'); await nextTick();
  expect(wrapper.get('[role="status"]').text()).toContain('已保存');
  vi.advanceTimersByTime(3999); await nextTick(); expect(toasts.value).toHaveLength(1);
  vi.advanceTimersByTime(1); await nextTick(); expect(wrapper.find('[role="status"]').exists()).toBe(false);
  toast.show('刷新完成'); await nextTick();
  await wrapper.get('button').trigger('click');
  expect(toasts.value).toHaveLength(0);
  toast.dismiss(id);
});
