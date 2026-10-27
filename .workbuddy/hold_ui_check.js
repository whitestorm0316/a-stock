/* 一次性集成验证：真实 index.html + 真实后端（127.0.0.1:8770）跑一遍
   「持仓盈亏」的 录入 → 渲染 → 删除 全链路。jsdom 注入 echarts/fetch 桩，
   不联网抓脚本。用后即弃，不进入正式测试套件。 */
const path = require('path');
const fs = require('fs');
const ROOT = path.resolve(__dirname, '..');
const { JSDOM, VirtualConsole } = require(path.join(ROOT, 'node_modules/jsdom'));

const BASE = process.env.CHECK_URL || 'http://127.0.0.1:8770';
const html = fs.readFileSync(path.join(ROOT, 'app/index.html'), 'utf8');
const errors = [];
let pass = 0, total = 0;
const ok = (cond, label, extra) => {
  total++;
  if (cond) { pass++; console.log('  [OK] ' + label); }
  else console.log('  [FAIL] ' + label + (extra ? '  -> ' + extra : ''));
};
const sleep = ms => new Promise(r => setTimeout(r, ms));

const vc = new VirtualConsole();
vc.on('jsdomError', e => errors.push('jsdomError: ' + (e && e.message)));
vc.on('error', (...a) => errors.push('console.error: ' + a.join(' ')));

const dom = new JSDOM(html, {
  runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
  url: BASE + '/',
  beforeParse(w) {
    w.HTMLElement.prototype.scrollIntoView = () => {};
    w.requestAnimationFrame = cb => setTimeout(() => cb(Date.now()), 0);
    // 图表替身：jsdom 没有 canvas，真 echarts.init 会炸；本用例只验持仓页
    w.echarts = { init: () => ({ setOption() {}, resize() {}, dispose() {},
                                 on() {}, getOption() { return {}; } }) };
    // jsdom 不自带 fetch：转成绝对地址后交给 Node 的实现
    w.fetch = (u, o) => fetch(new URL(u, BASE + '/').href, o);
    w.alert = m => errors.push('alert: ' + m);
    w.confirm = () => true;
  },
});

(async () => {
  const w = dom.window, d = w.document;
  const waitFor = async (fn, ms = 8000) => {
    const t0 = Date.now();
    while (Date.now() - t0 < ms) { if (fn()) return true; await sleep(120); }
    return false;
  };

  await sleep(1800);            // 等 init()（/api/defaults、市场序列）跑完

  // 1) Tab 存在
  const tab = [...d.querySelectorAll('#tabs button')].find(b => b.dataset.t === 'hold');
  ok(!!tab, '「持仓盈亏」Tab 已注册', tab ? '' : '未找到 data-t="hold"');
  if (!tab) process.exit(1);

  // 2) 切过去 → 表单 + 空态
  tab.click();
  ok(await waitFor(() => !!d.querySelector('#h-code')), '进入后渲染出录入表单');
  ok(!!d.querySelector('#h-save'), '保存按钮存在');
  ok((d.querySelector('#p-hold').textContent || '').includes('还没有录入持仓'),
     '空账本显示引导文案');

  // 3) 录入一笔
  d.querySelector('#h-code').value = '000001';
  d.querySelector('#h-cost').value = '10';
  d.querySelector('#h-shares').value = '1000';
  d.querySelector('#h-date').value = '2026-09-01';
  d.querySelector('#h-save').click();
  ok(await waitFor(() => !!d.querySelector('#h-tbl tbody tr')), '保存后出现明细行');

  const rowTxt = (d.querySelector('#h-tbl tbody tr') || {}).textContent || '';
  ok(rowTxt.includes('平安银行'), '自动补全了股票名称', rowTxt.slice(0, 80));
  ok(rowTxt.includes('14.70%') || rowTxt.includes('14.7%'),
     '盈亏率按 (11.47-10)/10=14.7% 算出', rowTxt.slice(0, 120));

  // 汇总卡片第一格 = 浮动盈亏
  const v1 = (d.querySelector('#p-hold .mgrid .mc .v') || {}).textContent || '';
  ok(/¥1,470\.00/.test(v1), '汇总「浮动盈亏」= ¥1,470.00', v1);

  // 4) 编辑按钮回填
  const editBtn = d.querySelector('[data-h-edit]');
  ok(!!editBtn, '明细行有「编辑」按钮');
  if (editBtn) {
    editBtn.click();
    await sleep(200);
    ok(d.querySelector('#h-cost').value === '10', '编辑回填成本价', d.querySelector('#h-cost').value);
    ok((d.querySelector('#h-save') || {}).textContent === '保存修改', '按钮切到「保存修改」');
    d.querySelector('#h-cancel').click();
    await sleep(150);
  }

  // 5) 删除 → 回到空态
  const delBtn = d.querySelector('[data-h-del]');
  ok(!!delBtn, '明细行有「删除」按钮');
  if (delBtn) {
    delBtn.click();
    ok(await waitFor(() => (d.querySelector('#p-hold').textContent || '')
       .includes('还没有录入持仓')), '删除后回到空账本');
  }

  // 6) 全程无未捕获异常
  ok(errors.length === 0, '无未捕获 JS 异常', errors.slice(0, 3).join(' | '));

  console.log(`\n${pass}/${total} 通过`);
  try { dom.window.close(); } catch (e) {}
  process.exit(pass === total ? 0 : 1);
})().catch(e => {
  console.log('[FAIL] 用例异常: ' + (e && e.stack || e));
  process.exit(1);
});
