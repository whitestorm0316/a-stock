/* 一次性集成验证：「策略持仓（回测自动）」区块 + 真实后端（127.0.0.1:8770）。
   jsdom 注入 echarts 桩、fetch 转真请求。用后即弃，不进正式测试套件。 */
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
    w.echarts = { init: () => ({ setOption() {}, resize() {}, dispose() {},
                                 on() {}, getOption() { return {}; } }) };
    w.fetch = (u, o) => fetch(new URL(u, BASE + '/').href, o);
    w.alert = m => errors.push('alert: ' + m);
    w.confirm = () => true;
  },
});

(async () => {
  await sleep(3000);
  const w = dom.window, d = w.document;
  const $ = s => d.querySelector(s), $$ = s => [...d.querySelectorAll(s)];
  const txt = el => (el ? el.textContent.replace(/\s+/g, ' ').trim() : '');
  const click = el => el.dispatchEvent(new w.MouseEvent('click', { bubbles: true }));

  // 进入「持仓盈亏」
  const tab = $$('#tabs button').find(b => b.dataset.t === 'hold');
  ok(!!tab, '找到「持仓盈亏」Tab');
  click(tab);
  await sleep(4000);            // /api/positions 约 1.4s

  const pane = $('#p-hold');
  ok(pane && pane.classList.contains('on'), '持仓 pane 已激活');

  const tbl = $('#p-tbl');
  ok(!!tbl, '策略持仓表 #p-tbl 已渲染');
  const rows = $$('#p-tbl tbody tr');
  ok(rows.length > 0, '策略持仓有行', '实际 ' + rows.length + ' 行');

  const heads = $$('#p-tbl thead th').map(x => txt(x).replace(/[▲▼]/g, ''));
  ok(heads.includes('预计卖出日'), '表头含「预计卖出日」', heads.join('|'));
  ok(heads.includes('还需'), '表头含「还需」（剩余交易日）', heads.join('|'));

  const r0 = rows[0] ? [...rows[0].children].map(x => txt(x)) : [];
  const DATE = /^\d{4}-\d{2}-\d{2}$/;
  ok(DATE.test(r0[5] || ''), '第一行「预计卖出日」是 YYYY-MM-DD', r0[5]);
  ok(/^\d+$/.test(r0[3] || ''), '已持有是整数交易日', r0[3]);
  ok(/^\d+$/.test(r0[4] || ''), '还需是整数交易日', r0[4]);
  // 已持有 + 还需 = H（20）
  ok(Number(r0[3]) + Number(r0[4]) === 20,
     '已持有 + 还需 = 持有期 20', r0[3] + '+' + r0[4]);
  // 浮动收益率 = 现价/买入价 − 1（复算前 3 行）
  let pctOk = true, pctMsg = '';
  for (const tr of rows.slice(0, 3)) {
    const c = [...tr.children].map(x => txt(x));
    const buy = parseFloat(c[6]), px = parseFloat(c[7]);
    const got = parseFloat(c[8]);
    const want = (px / buy - 1) * 100;
    if (!(Math.abs(got - want) < 0.02)) {
      pctOk = false;
      pctMsg = `${c[1]} 显示 ${got}% vs 复算 ${want.toFixed(2)}%`;
      break;
    }
  }
  ok(pctOk, '浮动收益率与 现价/买入价−1 一致（前三行）', pctMsg);

  // 按预计卖出日升序：第一行日期 <= 最后一行
  const dFirst = txt([...rows[0].children][5]);
  const dLast = txt([...rows[rows.length - 1].children][5]);
  ok(dFirst <= dLast, '默认按预计卖出日升序', dFirst + ' ≤ ' + dLast);

  // 汇总卡
  const cards = $$('#p-hold .btgrid .mc');
  ok(cards.length === 6, '策略持仓 6 张汇总卡', '实际 ' + cards.length);
  const cardTxt = cards.map(x => txt(x)).join(' || ');
  ok(/持有中/.test(cardTxt), '有「持有中」卡', cardTxt.slice(0, 120));
  ok(new RegExp(String(rows.length)).test(cardTxt), '持有笔数与表格行数一致');

  // 排序切换：点一次「浮动收益率」表头 → 换列并降序（最大在前）
  const thPct = $$('#p-tbl thead th').find(x => txt(x).startsWith('浮动收益率'));
  ok(!!thPct, '浮动收益率表头可点');
  if (thPct) {
    click(thPct); await sleep(300);
    const got = $$('#p-tbl tbody tr').map(tr => parseFloat(txt([...tr.children][8])));
    const head5 = got.slice(0, 5).join(', ');
    ok(Math.abs(got[0] - Math.max(...got.filter(x => !isNaN(x)))) < 1e-9,
       '点一次后降序、首行是收益率最高的一笔', '前5行 ' + head5);
    ok(Math.abs(got[got.length - 1] - Math.min(...got.filter(x => !isNaN(x)))) < 1e-9,
       '末行是收益率最低的一笔', '末行 ' + got[got.length - 1]);
  }

  // 手动账本：没有录入时应收起，且不影响策略持仓
  const fold = $('#p-hold details.hfold');
  ok(!!fold, '手动账本折叠区存在');
  ok(fold && !fold.open, '无手动持仓时默认收起');
  ok(!!$('#p-hold #h-tbl') === false, '空账本时不渲染手动明细表（符合预期）');

  ok(errors.length === 0, '全程无未捕获异常', errors.slice(0, 3).join(' | '));

  console.log('------------------------------------------------------------');
  console.log((pass === total ? '全部通过' : '有失败') + `：${pass}/${total}`);
  process.exit(pass === total ? 0 : 1);
})();
