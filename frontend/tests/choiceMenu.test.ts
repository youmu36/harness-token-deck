import { flushPromises, mount } from '@vue/test-utils';
import { defineComponent, h, ref } from 'vue';
import { expect, it, vi } from 'vitest';
import ChoiceMenu from '../src/ui/ChoiceMenu.vue';

it('reveals a time menu once on opening and restores focus without scrolling on selection',async () => {
  const reveal=vi.fn(), focus=vi.spyOn(HTMLElement.prototype,'focus');
  const original=HTMLElement.prototype.scrollIntoView;
  HTMLElement.prototype.scrollIntoView=reveal;
  const wrapper=mount(ChoiceMenu,{props:{modelValue:'a',label:'统计周期',revealOnOpen:true,options:[{value:'a',label:'本月'},{value:'b',label:'年度'}]}});
  try {
    await wrapper.get('.choice-trigger').trigger('click');await flushPromises();expect(reveal).toHaveBeenCalledTimes(1);
    await wrapper.findAll('[role="radio"]')[1]!.trigger('click');await flushPromises();
    expect(reveal).toHaveBeenCalledTimes(1);expect(focus).toHaveBeenLastCalledWith({preventScroll:true});
  }finally{wrapper.unmount();HTMLElement.prototype.scrollIntoView=original;focus.mockRestore();}
});

it('formats lazy choices only while open and reads fresh rows when reopened', async () => {
  const rows = ref(['会话1', '会话2']);
  const options = vi.fn(() => rows.value.map((label, index) => ({value: String(index), label: `${label}\n开始时间 进行中`})));
  const Host = defineComponent({setup: () => () => h(ChoiceMenu, {modelValue:'0',label:'柱状图视图',selectedLabel:'柱状图视图：会话1',options})});
  const wrapper = mount(Host);
  try {
    expect(options).not.toHaveBeenCalled();
    rows.value = ['会话1', '会话3']; await flushPromises();
    expect(options).not.toHaveBeenCalled();
    await wrapper.get('.choice-trigger').trigger('click');
    expect(options).toHaveBeenCalledTimes(1);
    expect(wrapper.findAll('[role="radio"]').at(1)!.text()).toContain('会话3');
    await wrapper.get('.choice-trigger').trigger('click');
    expect(wrapper.find('[role="radiogroup"]').exists()).toBe(false);
    rows.value = ['会话1', '会话4']; await flushPromises();
    expect(options).toHaveBeenCalledTimes(1);
    await wrapper.get('.choice-trigger').trigger('click');
    expect(options).toHaveBeenCalledTimes(2);
    await wrapper.findAll('[role="radio"]').at(1)!.trigger('click');
    expect(wrapper.findComponent(ChoiceMenu).emitted('update:modelValue')).toEqual([['1']]);
    expect(wrapper.find('[role="radiogroup"]').exists()).toBe(false);
    expect(wrapper.get('.choice-trigger').attributes('aria-label')).toBe('柱状图视图：会话1');
  } finally { wrapper.unmount(); }
});

it('retains static choices and the selected label for existing callers', async () => {
  const wrapper = mount(ChoiceMenu, {props:{modelValue:'b',label:'范围',options:[{value:'a',label:'今天'},{value:'b',label:'本月'}]}});
  try {
    expect(wrapper.get('.choice-value').text()).toBe('本月');
    await wrapper.get('.choice-trigger').trigger('keydown',{key:'ArrowDown'});
    expect(wrapper.findAll('[role="radio"]')).toHaveLength(2);
    await wrapper.get('.choice-trigger').trigger('keydown',{key:'Escape'});
    expect(wrapper.find('[role="radiogroup"]').exists()).toBe(false);
  } finally { wrapper.unmount(); }
});
