import { flushPromises, mount } from '@vue/test-utils';
import { expect,it } from 'vitest';
import MultiLineChart from '../src/ui/charts/MultiLineChart.vue';

it('shows every nearby series at the closest x coordinate and separates overlapping points',async () => {
  const series=['甲','乙'].map((name,index) => ({key:String(index),name,points:[{day:'2026-10-24',value:10+index*.001,label:`${10+index*.001} t/s`},{day:'2026-10-25',value:20,label:'20 t/s'}]}));
  const wrapper=mount(MultiLineChart,{props:{label:'TPS',unit:' t/s',series},attachTo:document.body});
  try {
    const svg=wrapper.get('svg');Object.defineProperty(svg.element,'getBoundingClientRect',{value:() => ({left:100,top:50,width:490,height:141})});
    await svg.trigger('mousemove',{clientX:100+80/2,clientY:50+100/2});
    expect(wrapper.get('[role="tooltip"]').text()).toContain('2026-10-24');
    expect(wrapper.findAll('[role="tooltip"] article').map(row => row.text())).toEqual(['甲10 t/s','乙10.001 t/s']);
    await svg.trigger('mousemove',{clientX:100+930/2,clientY:50+100/2});expect(wrapper.get('[role="tooltip"]').text()).toContain('2026-10-25');
    await wrapper.get('.plot').trigger('mouseleave');expect(wrapper.find('[role="tooltip"]').exists()).toBe(false);
    const first=wrapper.get('circle');await first.trigger('focus');expect(wrapper.get('[role="tooltip"]').text()).toContain('2026-10-24');
    await first.trigger('keydown',{key:'End'});await flushPromises();expect(document.activeElement).toBe(wrapper.get('[data-line="0"][data-point="1"]').element);
    await wrapper.get('[data-line="0"][data-point="1"]').trigger('keydown',{key:'Escape'});expect(wrapper.find('[role="tooltip"]').exists()).toBe(false);
    await svg.trigger('mousemove',{clientX:100,clientY:50});expect(wrapper.find('[role="tooltip"]').exists()).toBe(false);
  }finally{wrapper.unmount();}
});
