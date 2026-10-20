async page => {
  const checks = [];
  const check = (pass, label) => { if (!pass) throw new Error(label); checks.push(label); };
  const nav = name => page.getByRole('navigation', { name: '主导航' }).getByRole('link', { name, exact: true }).click();
  for (const theme of ['light', 'dark']) {
    await nav('设置');
    await page.getByRole('combobox', { name: '显示主题' }).selectOption(theme);
    await page.waitForFunction(t => document.documentElement.dataset.theme === t, theme);
    for (const width of [1440, 1024, 820]) {
      await page.setViewportSize({ width, height: 900 });
      for (const name of ['Codex', 'Zcode', 'DSH', '数据统计与分析', '价格与模型', '供应商与钱包']) {
        await nav(name);
        await page.locator('main h1').waitFor();
        check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth && document.documentElement.scrollHeight <= innerHeight + 1), `${theme}/${width}/${name}: viewport bounded`);
        if (['Codex','Zcode','DSH'].includes(name)) {
          check(await page.getByRole('region', { name: '用量趋势', exact: true }).count() === 0, `${theme}/${width}/${name}: no trend`);
          check(await page.locator('.collection-meta,.balance-detail').count() === 0, `${theme}/${width}/${name}: compact header`);
        }
        if (name === '数据统计与分析') check(await page.getByRole('region', { name: '用量趋势', exact: true }).count() === 1, `${theme}/${width}: analysis trend`);
        if (name === '价格与模型') check(await page.getByRole('combobox', { name: '价格组关联供应商' }).isVisible(), `${theme}/${width}: vendor link primary`);
        if (name === '供应商与钱包') check(await page.getByRole('button', { name: '关联价格组', exact: true }).isVisible(), `${theme}/${width}: group link primary`);
        await page.locator('.workspace').evaluate(el => { el.scrollTop = el.scrollHeight; });
        const bottom = await page.evaluate(() => {
          const host = document.querySelector('.workspace');
          const sidebar = document.querySelector('.sidebar').getBoundingClientRect();
          return Math.abs(host.scrollTop + host.clientHeight - host.scrollHeight) < 2 && sidebar.top === 0 && sidebar.bottom <= innerHeight + 1;
        });
        check(bottom, `${theme}/${width}/${name}: scroll ends within page`);
        await page.locator('.workspace').evaluate(el => { el.scrollTop = 0; });
        if (width === 1440 && ['Codex','价格与模型','供应商与钱包'].includes(name)) {
          const slug = { Codex:'codex', '价格与模型':'pricing', '供应商与钱包':'wallet' }[name];
          await page.screenshot({ path: `docs/design/screens/feedback-${theme}-${slug}.png`, animations:'disabled' });
        }
      }
    }
  }
  await page.setViewportSize({ width:1280, height:800 });
  await nav('价格与模型');
  await page.getByRole('textbox', { name:'倍率', exact:true }).fill('1.75');
  await page.getByRole('button', { name:'保存倍率提示', exact:true }).click();
  await page.getByRole('button', { name:'价格组设置', exact:true }).click();
  await page.getByRole('button', { name:'价格编辑', exact:true }).click();
  check(await page.getByRole('textbox', { name:'倍率', exact:true }).inputValue() === '1.75', 'multiplier survives editor remount');
  for (let i=0;i<7;i++) await page.getByRole('button', {name:'添加模型行',exact:true}).click();
  await page.locator('.workspace').evaluate(el => { el.scrollTop = 500; });
  const rail = await page.locator('.management-rail').boundingBox();
  check(rail.y >= 0 && rail.y < 50, 'price group rail remains visible while editor scrolls');
  await page.getByRole('button',{name:'放弃修改',exact:true}).click();
  await page.getByRole('dialog').getByRole('button',{name:'确认',exact:true}).click();
  await nav('Codex');
  await page.getByRole('button',{name:'重算历史…',exact:true}).click();
  check(await page.getByRole('combobox',{name:'重算价格分组'}).count() === 1 && await page.getByRole('combobox',{name:'重算目标版本'}).count() === 1, 'recalc group and version columns');
  check(!(await page.getByRole('combobox',{name:'重算目标版本'}).innerText()).includes('秋季价格 · v'), 'custom version label has no numeric suffix');
  await page.getByRole('button',{name:'打开日历',exact:true}).first().click();
  await page.getByRole('button',{name:'小时',exact:true}).click();
  check(await page.getByRole('listbox',{name:'小时',exact:true}).isVisible(), 'time opens a scroll list');
  await page.getByRole('option',{name:'06',exact:true}).click();
  await page.keyboard.press('Escape');
  await page.getByRole('dialog',{name:'重算历史',exact:true}).getByRole('button',{name:'取消',exact:true}).click();
  await nav('供应商与钱包');
  await page.getByRole('button',{name:'资金流水',exact:true}).click();
  await page.getByRole('combobox',{name:'流水类型'}).selectOption('top_up');
  check(!(await page.locator('.entries tbody').innerText()).includes('消费日汇总'), 'entry filter precedes presentation');
  return { ok:true, count:checks.length, checks };
}
