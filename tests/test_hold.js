/* 前端「持仓盈亏」Tab 验证（jsdom 无头，离线）
 *
 * 覆盖：Tab 注册 → 空态 → 客户端校验 → 录入 → 汇总/明细渲染 → 无行情告警
 *       → 表头排序 → 编辑回填与保存 → 删除回空态 → 全程无未捕获异常
 *
 * ⚠️ 账本用**内存桩**（不碰真实 data/holdings.json）：桩的返回结构必须与
 *    server.py 的 /api/holdings 完全一致（字段名、单位、百分数口径），
 *    否则测的是「桩自己能跑」而不是「页面能跑」。
 * ⚠️ 价格用固定值（000001=11.47 / 600000=9.82），断言才能写精确到分的数字。
 */
const fs = require('fs');
const path = require('path');
const ROOT = path.resolve(__dirname, '..');
const HTML_PATH = process.env.SMOKE_HTML || path.join(ROOT, 'app/index.html');
const html = fs.readFileSync(HTML_PATH, 'utf8');
const { JSDOM } = require(path.join(ROOT, 'node_modules/jsdom'));
const F = n => JSON.parse(fs.readFileSync(path.join(__dirname, `fixtures/fixture_${n}.json`), 'utf8'));
const FX = { meta: F('meta'), defaults: F('defaults'), backtest: F('backtest'), scan: F('scan') };

/* ---------------- 内存账本桩 ---------------- */
const ASOF = '2026-10-27';
const PX = {
  '000001': { code: '000001.SZ', name: '平安银行', price: 11.47, prev: 11.30 },
  '600000': { code: '600000.SH', name: '浦发银行', price: 9.82, prev: 9.62 },
};
const state = { lots: [], hid: 0 };
const saveCalls = [], delCalls = [], getCalls = [];
const r = (v, n = 2) => Math.round(v * 10 ** n) / 10 ** n;
const norm6 = s => (String(s || '').match(/\d{6}/) || [''])[0];

function holdingsResp() {
  const rows = state.lots.map(l => {
    const p = PX[norm6(l.code)];
    const costAmt = l.cost * l.shares;
    const base = {
      id: l.id, code: p ? p.code : l.code, name: l.name || (p ? p.name : ''),
      cost: l.cost, shares: l.shares, cost_amt: r(costAmt),
      buy_date: l.buy_date || null, note: l.note || '', ind: '银行', board: 'MAIN',
    };
    if (!p) {
      return Object.assign(base, {
        noquote: true, price: null, price_date: null, chg: null, stale_days: null,
        price_kind: null, mv: null, pnl: null, pnl_pct: null, day_pnl: null, hold_days: null,
      });
    }
    const mv = p.price * l.shares, pnl = mv - costAmt;
    return Object.assign(base, {
      noquote: false, price: p.price, price_date: ASOF,
      chg: r(p.price / p.prev - 1, 6), stale_days: 0, price_kind: 'raw',
      mv: r(mv), pnl: r(pnl), pnl_pct: r(pnl / costAmt, 6),
      day_pnl: r((p.price - p.prev) * l.shares),
      hold_days: l.buy_date
        ? Math.max(0, Math.round((Date.parse(ASOF) - Date.parse(l.buy_date)) / 86400000))
        : null,
    });
  });
  const priced = rows.filter(x => !x.noquote);
  const cost = priced.reduce((a, b) => a + b.cost * b.shares, 0);
  const mv = priced.reduce((a, b) => a + b.mv, 0);
  const day = priced.reduce((a, b) => a + (b.day_pnl || 0), 0);
  const base = mv - day;
  return {
    ok: true, at: ASOF, rows,
    totals: {
      n_lot: rows.length, n_stock: rows.length, n_priced: priced.length,
      n_win: priced.filter(x => x.pnl > 0).length,
      n_loss: priced.filter(x => x.pnl < 0).length,
      cost: r(cost), mv: r(mv), pnl: r(mv - cost),
      pnl_pct: cost > 0 ? r((mv - cost) / cost, 6) : null,
      day_pnl: r(day), day_base: r(base),
      day_pct: base > 0 ? r(day / base, 6) : null,
    },
    n_noquote: rows.length - priced.length,
    noquote: rows.filter(x => x.noquote).map(x => x.code),
  };
}

const errors = [];
const dom = new JSDOM(html, {
  runScripts: 'dangerously', pretendToBeVisual: true,
  url: 'http://127.0.0.1:8771/',
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
      const body = opt && opt.body ? JSON.parse(opt.body) : {};
      if (u.includes('/api/holdings/save')) {
        saveCalls.push(body);
        if (!(body.cost > 0)) return reply({ ok: false, msg: '成本价必须大于 0' });
        if (!(body.shares > 0)) return reply({ ok: false, msg: '股数必须大于 0' });
        const p = PX[norm6(body.code)];
        const rec = {
          id: body.id || `H${++state.hid}`, code: p ? p.code : norm6(body.code),
          name: p ? p.name : '', cost: body.cost, shares: body.shares,
          buy_date: body.buy_date || '', note: body.note || '',
        };
        if (body.id) {
          const i = state.lots.findIndex(x => x.id === body.id);
          if (i < 0) return reply({ ok: false, msg: '该持仓记录已不存在，请刷新' });
          state.lots[i] = rec;
        } else {
          state.lots.push(rec);
        }
        return reply({ ok: true, msg: body.id ? '已更新这笔持仓' : '已添加', id: rec.id });
      }
      if (u.includes('/api/holdings/delete')) {
        delCalls.push(body);
        state.lots = state.lots.filter(x => x.id !== body.id);
        return reply({ ok: true, msg: '已删除' });
      }
      if (u.includes('/api/holdings')) { getCalls.push(u); return reply(holdingsResp()); }
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
const txt = el => (el ? el.textContent : '');

(async () => {
  await sleep(2600);
  const out = [];
  const ok = (t, c, x) => out.push(`${c ? '✅' : '❌'} ${t}${x !== undefined ? '  → ' + x : ''}`);

  // ---- 1. Tab / Pane
  const tabs = $$('#tabs button').map(b => b.dataset.t);
  ok('Tab 含 hold', tabs.includes('hold'), tabs.join(','));
  ok('持仓 pane 存在', !!$('#p-hold'));
  ok('Tab 总数 = 7', tabs.length === 7, '实际 ' + tabs.length);

  // ---- 2. 进入 → 空态
  click($$('#tabs button').find(b => b.dataset.t === 'hold'));
  await sleep(500);
  ok('进入后 pane 激活', $('#p-hold').classList.contains('on'));
  ok('自动拉取 /api/holdings', getCalls.length >= 1, '调用 ' + getCalls.length + ' 次');
  ok('空账本显示引导文案', txt($('#p-hold')).includes('还没有录入持仓'));
  ok('录入表单已渲染', !!$('#h-code') && !!$('#h-save'));

  // ---- 3. 客户端校验：成本价为空 → 不发请求
  $('#h-code').value = '000001';
  click($('#h-save'));
  await sleep(120);
  ok('成本价为空时拦截', saveCalls.length === 0, '已发 ' + saveCalls.length + ' 次');
  ok('提示文案指向成本价', txt($('#h-msg')).includes('成本价'), txt($('#h-msg')));

  // ---- 4. 录入第 1 笔
  $('#h-code').value = '000001';
  $('#h-cost').value = '10';
  $('#h-shares').value = '1000';
  $('#h-date').value = '2026-09-01';
  click($('#h-save'));
  await sleep(500);
  ok('保存请求体原样提交 6 位代码', saveCalls[0] && saveCalls[0].code === '000001',
     JSON.stringify(saveCalls[0] || {}));
  ok('明细出现 1 行', $$('#h-tbl tbody tr').length === 1,
     '实际 ' + $$('#h-tbl tbody tr').length);
  ok('自动补全名称', txt($$('#h-tbl tbody tr')[0]).includes('平安银行'));
  // 盈亏率 14.70% = (11.47-10)/10
  ok('盈亏率 = 14.70%', txt($$('#h-tbl tbody tr')[0]).includes('14.70%'),
     txt($$('#h-tbl tbody tr')[0]).slice(0, 120));
  ok('汇总浮动盈亏 = ¥1,470.00', txt($('#p-hold .mgrid .mc .v')) === '¥1,470.00',
     txt($('#p-hold .mgrid .mc .v')));

  // ---- 5. 录入第 2 笔（不同票）
  $('#h-code').value = '600000';
  $('#h-cost').value = '9.5';
  $('#h-shares').value = '500';
  $('#h-date').value = '';
  click($('#h-save'));
  await sleep(500);
  ok('明细出现 2 行', $$('#h-tbl tbody tr').length === 2,
     '实际 ' + $$('#h-tbl tbody tr').length);
  // 1470 + (9.82-9.5)*500 = 1470 + 160 = 1630
  ok('合计浮动盈亏 = ¥1,630.00', txt($('#p-hold .mgrid .mc .v')) === '¥1,630.00',
     txt($('#p-hold .mgrid .mc .v')));

  // ---- 6. 无行情代码：允许保存，但计入告警、不进合计
  $('#h-code').value = '999999';
  $('#h-cost').value = '5';
  $('#h-shares').value = '100';
  click($('#h-save'));
  await sleep(500);
  ok('无行情代码仍可保存', state.lots.length === 3);
  ok('出现无行情告警框', !!$('#p-hold .warnbox') && txt($('#p-hold .warnbox')).includes('999999'),
     txt($('#p-hold .warnbox')).slice(0, 60));
  ok('无行情不计入合计', txt($('#p-hold .mgrid .mc .v')) === '¥1,630.00',
     txt($('#p-hold .mgrid .mc .v')));
  ok('无行情行标灰但可见', !!$('#h-tbl tr.hq-no') && txt($('#h-tbl tr.hq-no')).includes('999999'));

  // ---- 7. 表头排序：默认盈亏率降序 → 点一次变升序
  const th = $('#h-tbl th[data-k="pnl_pct"]');
  ok('盈亏率表头可点', !!th);
  click(th);
  await sleep(250);
  ok('升序后最小盈亏率排第一', txt($$('#h-tbl tbody tr')[0]).includes('浦发银行'),
     txt($$('#h-tbl tbody tr')[0]).slice(0, 60));

  // ---- 8. 亏损批次：金额要显示成 -¥218.00（负号在货币符号前）
  $('#h-code').value = '600000';
  $('#h-cost').value = '12';
  $('#h-shares').value = '100';
  click($('#h-save'));
  await sleep(500);
  const lossRow = $$('#h-tbl tbody tr').find(tr => txt(tr).includes('-¥218.00'));
  ok('亏损显示为 -¥218.00（负号在 ¥ 前）', !!lossRow,
     lossRow ? txt(lossRow).replace(/\s+/g, ' ').slice(0, 60) : '未找到 -¥218.00');
  // -218/1200 = -18.17%
  ok('亏损率带负号', !!lossRow && txt(lossRow).includes('-18.17%'),
     lossRow ? txt(lossRow).replace(/\s+/g, ' ').slice(0, 90) : '');

  // ---- 9. 编辑回填 + 保存带 id
  const row1 = $$('#h-tbl tbody tr').find(tr => txt(tr).includes('平安银行'));
  click(row1.querySelector('[data-h-edit]'));
  await sleep(250);
  ok('编辑回填成本价', $('#h-cost').value === '10', $('#h-cost').value);
  ok('按钮切到「保存修改」', txt($('#h-save')) === '保存修改', txt($('#h-save')));
  $('#h-cost').value = '9.5';
  click($('#h-save'));
  await sleep(500);
  const last = saveCalls[saveCalls.length - 1] || {};
  ok('修改请求带 id', String(last.id || '').startsWith('H'), JSON.stringify(last));
  ok('修改后成本价生效', last.cost === 9.5, String(last.cost));

  // ---- 10. 逐笔删除 → 回到空态（共 4 笔：000001、600000、999999、亏损批）
  for (let i = 0; i < 4; i++) {
    const b = $('#h-tbl [data-h-del]');
    if (!b) break;
    click(b);
    await sleep(400);
  }
  ok('删除请求发出 4 次', delCalls.length === 4, '实际 ' + delCalls.length);
  ok('删除后回到空账本', txt($('#p-hold')).includes('还没有录入持仓'));

  // ---- 11. 全程无未捕获异常
  ok('无未捕获 JS 异常', errors.length === 0, errors.slice(0, 3).join(' | '));

  console.log('\n══════ 前端「持仓盈亏」Tab 验证 ══════\n');
  out.forEach(l => console.log(l));
  const pass = out.filter(l => l.startsWith('✅')).length;
  console.log(`\n通过 ${pass}/${out.length}`);
  process.exit(0);
})();
