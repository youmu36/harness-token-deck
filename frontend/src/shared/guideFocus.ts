import { nextTick, type Directive } from 'vue';

const focused = new WeakSet<HTMLElement>();
async function focus(el: HTMLElement, enabled: boolean) {
  if (!enabled) { focused.delete(el); el.classList.remove('guide-target'); return; }
  await nextTick();
  if (focused.has(el) || el.matches(':disabled')) return;
  focused.add(el);
  el.classList.add('guide-target');
  el.scrollIntoView?.({ block: 'center', behavior: 'smooth' });
  el.focus({ preventScroll: true });
}
export const vGuideFocus: Directive<HTMLElement, boolean> = {
  mounted: (el, binding) => { void focus(el, binding.value); },
  updated: (el, binding) => { void focus(el, binding.value); },
};
