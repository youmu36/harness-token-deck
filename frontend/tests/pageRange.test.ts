import { mount, flushPromises } from '@vue/test-utils';
import { reactive } from 'vue';
import { afterEach, expect, it, vi } from 'vitest';
import { defaultAnalysisView } from '../src/features/overview/analysisState';
import { usePageRange } from '../src/features/overview/pageRange';
import PageRangeBar from '../src/features/overview/PageRangeBar.vue';
afterEach(() => vi.useRealTimers());

it('keeps the recent range while browsing historic quarters and years', async () => {
  vi.useFakeTimers(); vi.setSystemTime(new Date('2026-10-25T08:00:00Z'));
  const view = reactive(defaultAnalysisView()); view.period = 'range'; view.recent.range = '7days';
  const time = usePageRange(view,() => 'UTC'), initial = time.range.value;
  view.period = 'quarter'; view.year = 2024; view.quarter = 1;
  expect(time.range.value).toMatchObject({start:'2024-01-01T00:00:00.000Z',end:'2024-04-01T00:00:00.000Z'});
  const wrapper = mount(PageRangeBar,{props:{view,range:time.range.value,recentRange:time.recent.value,mode:'UTC'}});
  try { expect(wrapper.get('fieldset').attributes('disabled')).toBeDefined();
    view.period = 'range'; await wrapper.setProps({range:time.range.value}); await flushPromises();
    expect(wrapper.get('fieldset').attributes('disabled')).toBeUndefined(); expect(time.range.value).toEqual(initial);
  } finally { wrapper.unmount(); }
});
it('advances a live query without changing its dependency range and retains exact custom instants', () => {
  vi.useFakeTimers(); vi.setSystemTime(new Date('2026-10-25T08:00:00Z'));
  const view = reactive(defaultAnalysisView()); view.period = 'range'; const time = usePageRange(view,() => 'UTC');
  const initial = time.range.value;
  vi.setSystemTime(new Date('2026-10-25T08:05:00Z'));
  expect(time.advance().end).toBe('2026-10-25T08:05:00.000Z'); expect(time.range.value).toEqual(initial);
  view.recent = {...view.recent,range:'custom',start:'2026-10-01T00:00:00.000Z',end:'2026-10-02T00:00:00.000Z'};
  expect(time.advance()).toMatchObject({start:view.recent.start,end:view.recent.end});
});
