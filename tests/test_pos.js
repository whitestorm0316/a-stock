/* 前端「策略持仓」（回测里持有期没走完的头寸）验证（jsdom 无头，离线）
 *
 * 覆盖：Tab 注册 → 策略持仓表渲染 → 列口径（已持有+还需=H）→ **预计卖出日** →
 *       浮动收益率文本 → 汇总卡 → 表头排序 → 手动账本默认折叠 → 无未捕获异常
 *
 * ⚠️ 桩的返回结构必须与 server.py 的 /api/positions 完全一致（字段名、单位）。
 * ⚠️ 价格/收益率用固定值，断言才能写精确到分的百分比。
 */
const fs = require('fs');
const path = require('path');
const ROOT = path.resolve(__dirname, '..');
const HTML_PATH = process.env.SMOKE_HTML || path.join(ROOT, 'app/index.html');
const html = fs.readFileSync(HTML_PATH, 'utf8');
const { JSDOM } = require(path.join(ROOT, 'node_modules/jsdom'));
const F = n => JSON.parse(fs.readFileSync(path.join(__dirname, `fixtures/fixture_${n}.json`), 'utf8'));
const FX = { meta: F('meta'), defaults: F('defaults'), backtest: F('backtest'), scan: F('scan') };

/* ---------------- 策略持仓桩：3 笔，覆盖「未到期 / 中途 / 明天卖」 ------------- */
const ASOF = '2026-10-27';
const POS_RESP = {
  ok: true, at: ASOF, hold: 20, ms: 1200,
  rows: [
    { code: '000001.SZ', name: '平安银行', ind: '银行', board: 'MAIN',
      signal_date: '2026-10-12', entry_date: '2026-10-13',
      buy: 10.0, price: 11.47, price_date: ASOF, stale_days: 0,
      pnl_pct: 0.147, day_chg: 0.015044,
      hold: 20, hold_days: 11, left_days: 9, exit_est: '2026-11-10', pending: false },
    { code: '600000.SH', name: '浦发银行', ind: '银行', board: 'MAIN',
      signal_date: '2026-10-22', entry_date: '2026-10-23',
      buy: 12.0, price: 9.82, price_date: ASOF, stale_days: 0,
      pnl_pct: -0.181667, day_chg: 0.020792,
      hold: 20, hold_days: 3, left_days: 17, exit_est: '2026-11-20', pending: false },
    { code: '300750.SZ', name: '宁德时代', ind: '电池', board: 'GEM',
      signal_date: '2026-09-29', entry_date: '2026-09-30',
      buy: 100.0, price: 105.0, price_date: ASOF, stale_days: 0,
      pnl_pct: 0.05, day_chg: -0.004,
      hold: 20, hold_days: 20, left_days: 0, exit_est: '2026-10-28', pending: false },
  ],
  totals: {
    n: 3, n_hold: 3, n_pending: 0, n_stock: 3, n_win: 2, n_loss: 1,
    avg_pct: 0.005111, med_pct: 0.05, best: 0.147, worst: -0.181667,
    next_exit: '2026-10-28', hold: 20, capped: false,
  },
};

const posCalls = [];
const errors = [];
const dom = new JSDOM(html, {
  runScripts: 'dangerously', pretendToBeVisual: true,
  url: 'http://127.0.0.1:8772/',
  beforeParse(w) {
    w.HTMLElement.prototype.scrollIntoView = () => {};
    w.requestAnimationFrame = cb => setTimeout(() => cb(Date.now()), 0);
    w.echarts = { init: () => ({ setOption() {}, resize() {}, dispose() {},
                                 on() {}, getOption() { return {}; } }) };
    const reply = body => ({
      ok: true, status: 200, headers: { get: () => null },
      json: async () => body, text: async () => JSON.stringify(body),
      blob: async () => ({ size: 3 }), arrayBuffer: async () => new ArrayBuffer(3),
    });
    w.fetch = async (url, opt) => {
      const u = String(url);
      if (u.includes('/api/positions')) {
        posCalls.push(opt && opt.body ? JSON.parse(opt.body) : {});
        return reply(POS_RESP);
      }
      // 账本空：只验证策略持仓那块，账本走空态
      if (u.includes('/api/holdings')) {
        return reply({ ok: true, at: ASOF, rows: [],
                       totals: { n_lot: 0, n_stock: 0, n_priced: 0, n_win: 0, n_loss: 0,
                                 cost: 0, mv: 0, pnl: 0, pnl_pct: null,
                                 day_pnl: 0, day_base: 0, day_pct: null },
                       n_noquote: 0, noquote: [] });
      }
      if (u.includes('/api/meta')) return reply(FX.meta);
      if (u.includes('/api/defaults')) return reply(FX.defaults);
      if (u.includes('/api/backtest')) return reply(FX.backtest);
      if (u.includes('/api/scan')) return reply(FX.scan);
      return reply({});
    };
    w.alert = m => errors.push('alert: ' + m);
    w.confirm = () => true;
    w.onerror = (m, s, l, c, e) => errors.push(`${m} @${l}:${c}`);
    w.console.error = (...a) => errors.push(a.join(' '));
  },
});

const w = dom.window, d = w.document;
const $ = s => d.querySelector(s), $$ = s => [...d.querySelectorAll(s)];
const click = el => el.dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
const sleep = ms => new Promise(res => setTimeout(res, ms));
const txt = el => (el ? el.textContent.replace(/\s+/g, ' ').trim() : '');
const cell = (tr, i) => txt([...tr.children][i]);

(async () => {
  await sleep(2600);
  const out = [];
  const ok = (t, c, x) => out.push(`${c ? '✅' : '❌'} ${t}${x !== undefined ? '  → ' + x : ''}`);

  // ---- 1. 进入持仓页
  const tab = $$('#tabs button').find(b => b.dataset.t === 'hold');
  ok('Tab 含 hold', !!tab);
  click(tab);
  await sleep(1200);

  ok('一次请求 /api/positions', posCalls.length === 1, '实际 ' + posCalls.length);
  ok('请求带 params（随当前参数联动）',
     !!(posCalls[0] && posCalls[0].params), JSON.stringify(posCalls[0]).slice(0, 80));

  const tbl = $('#p-tbl');
  ok('策略持仓表已渲染', !!tbl);
  const rows = $$('#p-tbl tbody tr');
  ok('行数 = 3', rows.length === 3, '实际 ' + rows.length);

  // ---- 2. 列口径
  const heads = $$('#p-tbl thead th').map(x => txt(x).replace(/[▲▼]/g, ''));
  ok('表头含「预计卖出日」', heads.includes('预计卖出日'), heads.join('|'));
  ok('表头含「还需」', heads.includes('还需'), heads.join('|'));
  ok('表头含「持有进度」', heads.includes('持有进度'), heads.join('|'));

  // 默认按预计卖出日升序 → 最早到期的 300750（2026-10-28）在最前
  ok('默认按预计卖出日升序，首行是最早到期的那笔',
     cell(rows[0], 5) === '2026-10-28', cell(rows[0], 5));
  ok('预计卖出日格式为 YYYY-MM-DD', /^\d{4}-\d{2}-\d{2}$/.test(cell(rows[0], 5)), cell(rows[0], 5));
  ok('还需=0 的到期日高亮（xdue）',
     [...rows[0].children][5].classList.contains('xdue'),
     [...rows[0].children][5].className);

  // 每一行都满足「已持有 + 还需 = H(20)」
  const sumOk = rows.every(tr => Number(cell(tr, 3)) + Number(cell(tr, 4)) === 20);
  ok('每行 已持有 + 还需 = 持有期 20', sumOk,
     rows.map(tr => cell(tr, 3) + '+' + cell(tr, 4)).join(', '));

  // ---- 3. 数值渲染
  const rowBy = code => rows.find(tr => cell(tr, 0) === code);
  const rpb = rowBy('000001.SZ');
  ok('平安银行 买入价 10.00', rpb && cell(rpb, 6) === '10.00', rpb && cell(rpb, 6));
  ok('平安银行 现价 11.47', rpb && cell(rpb, 7) === '11.47', rpb && cell(rpb, 7));
  // PCT() 对正数**不加** + 号（与全站其他表格一致），所以断言写 14.70%
  ok('平安银行 浮动收益率 14.70%', rpb && cell(rpb, 8) === '14.70%', rpb && cell(rpb, 8));
  const rpf = rowBy('600000.SH');
  ok('浦发银行 亏损率带负号 -18.17%', rpf && cell(rpf, 8) === '-18.17%', rpf && cell(rpf, 8));
  ok('亏损用 .neg（绿）', rpf && [...rpf.children][8].classList.contains('neg'),
     rpf && [...rpf.children][8].className);
  ok('盈利用 .pos（红）', rpb && [...rpb.children][8].classList.contains('pos'),
     rpb && [...rpb.children][8].className);

  // ---- 4. 汇总卡
  const cards = $$('#p-hold .btgrid .mc').map(txt);
  ok('6 张汇总卡', cards.length === 6, '实际 ' + cards.length);
  ok('「持有中」显示 3 笔', cards.some(c => c.includes('持有中') && c.includes('3 笔')),
     cards.find(c => c.includes('持有中')));
  ok('「最快到期」= 2026-10-28',
     cards.some(c => c.includes('最快到期') && c.includes('2026-10-28')),
     cards.find(c => c.includes('最快到期')));
  ok('赚钱占比 66.7%', cards.some(c => c.includes('66.7%')),
     cards.find(c => c.includes('赚钱占比')));

  // ---- 5. 排序：点「浮动收益率」→ 换列并降序
  const thPct = $$('#p-tbl thead th').find(x => txt(x).startsWith('浮动收益率'));
  ok('浮动收益率表头可点', !!thPct);
  click(thPct);
  await sleep(150);
  const r0 = $$('#p-tbl tbody tr');
  ok('降序：首行 14.70%（最高）', cell(r0[0], 8) === '14.70%', cell(r0[0], 8));
  ok('末行 -18.17%（最低）', cell(r0[2], 8) === '-18.17%', cell(r0[2], 8));

  // 再点一次 → 升序
  const thPct2 = $$('#p-tbl thead th').find(x => txt(x).startsWith('浮动收益率'));
  click(thPct2);
  await sleep(150);
  ok('再点一次转升序，首行 -18.17%', cell($$('#p-tbl tbody tr')[0], 8) === '-18.17%',
     cell($$('#p-tbl tbody tr')[0], 8));

  // ---- 6. 手动账本默认折叠，且策略持仓不被它影响
  const fold = $('#p-hold details.hfold');
  ok('手动账本折叠区存在', !!fold);
  ok('无手动持仓时默认收起', fold && !fold.open);
  ok('折叠区里有录入表单（仍可用）', !!$('#p-hold #h-code'));
  ok('策略表不在折叠区内（置顶展示）',
     !!$('#p-tbl') && !fold.contains($('#p-tbl')));

  // ---- 7. 无未捕获异常
  ok('无未捕获 JS 异常', errors.length === 0, errors.slice(0, 3).join(' | '));

  console.log(out.join('\n'));
  const bad = out.filter(x => x.startsWith('❌'));
  console.log('------------------------------------------------------------');
  // ⚠️ run.sh 用 `grep -oE '[0-9]+/[0-9]+'` 解析，必须输出 N/M 形式
  console.log(bad.length === 0
    ? `✅ 全部通过：${out.length}/${out.length}`
    : `❌ 通过 ${out.length - bad.length}/${out.length}`);
  process.exit(bad.length === 0 ? 0 : 1);
})();
