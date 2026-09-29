/* 单股透视验证（jsdom 无头）。
 *
 * 背景（两次真实用户提问触发的）：
 *   Q1「主面板上 001256.SZ 在 9月15号有信号，怎么在单股透视上看不到？」
 *   Q2「单股透视 我还是希望能看到所有历史的」
 *
 *   根因 A —— **这一页用的是另一套参数**：
 *     · 主面板走 `collect() + currentPool()`，把左侧市值区间（用户当时是「最小40%」）
 *       和股票池（主板 + 沪深）一起发给后端 → 该股在 2026-09-15 命中。
 *     · 单股透视当时是 GET `/api/stock?code=`，后端 `stock_detail()` **写死**
 *       `build_mask(DEFAULT_PARAMS, None)` —— 市值仍是 0~2（最小30%）、不限池
 *       → 同一只票在这页算不出信号。
 *     该股 2026-09-15 的 size_grp=3（30%~40% 档），刚好被「最小30%」挡在门外，
 *     所以是 100% 必然复现，不是随机。
 *
 *   根因 B —— **信号表被裁到图表窗口**（这是修 Q1 时我自己引入的过度修复）：
 *     `rows` 只取最近 250 个交易日。我第一版把 `signal_dates` 也裁到窗口内，
 *     结果用户「看不到所有历史」了 —— 全历史 7 次信号里只有 1 次在窗口内，
 *     另外 6 次（2023 年）被悄悄丢掉。
 *     ✅ 正解：信号表**列全历史**，图表默认近 250 日 + 提供「全部历史」切换；
 *        不在图上的信号 **标灰 + 注明「图外」**，绝不隐藏。
 *
 * 覆盖点：
 *   1. 请求形态：必须 POST，且 body 里带 code/params/pool（不能再用 GET 走默认）
 *   2. params 确实跟随左侧（改市值区间 → 发出去的 size_max 跟着变）
 *   3. pool 确实跟随左侧（改板块 → 发出去的 boards 跟着变）
 *   4. 渲染「本页所用条件」，且条件文案来自后端（不前端另造一份）
 *   5. **信号表列全历史**（含窗口外的信号），且窗口外的行有「图外」标记
 *   6. 「全部历史」按钮存在；点击后发 full_history=true 并重绘（数据源切成 hist_rows）
 *   7. 年份筛选可用
 *   8. 无条件命中时给出可操作的提示（而不是一句「未触发默认条件」）
 *   9. 无运行时错误
 */
const fs = require('fs');
const path = require('path');
const ROOT = path.resolve(__dirname, '..');
/* ⚠️ 允许 `SMOKE_HTML=<备用 index.html>` 覆盖被测文件 —— 用于**变异测试**
   （把修复点回退，确认新断言真的会变红；见 dashboard-data-consistency 的 Rule 5）。
   默认仍是仓库里的 app/index.html。 */
const HTML_PATH = process.env.SMOKE_HTML || path.join(ROOT, 'app/index.html');
const html = fs.readFileSync(HTML_PATH, 'utf8');
const { JSDOM } = require(path.join(ROOT, 'node_modules/jsdom'));
const F = n => JSON.parse(fs.readFileSync(path.join(__dirname, `fixtures/fixture_${n}.json`), 'utf8'));
const FX = {
  meta: F('meta'), defaults: F('defaults'), scan: F('scan'),
  market: F('market'), backtest: F('backtest'), stock: F('stock'),
  stock_full: F('stock_full'),
};

const errors = [];
const reqs = [];

/* mock /api/stock —— 关键：**回显请求里的条件**，这样「前端发了什么」可断言。
   ⚠️ 若 mock 恒返回同一个 fixture，第 2/3 条断言就形同虚设
      （改参数也测不出「有没有跟着变」）。
   另外：带 `full_history: true` 的请求返回 fixture_stock_full（含 hist_rows）——
   这正是前端「全部历史」按钮走的懒加载分支。 */
function stockBody(payload) {
  const full = !!(payload && payload.full_history);
  const b = JSON.parse(JSON.stringify(full ? FX.stock_full : FX.stock));
  b._echo = { params: (payload && payload.params) || null, pool: (payload && payload.pool) || null };
  return b;
}

const dom = new JSDOM(html, {
  runScripts: 'dangerously',
  pretendToBeVisual: true,
  url: 'http://127.0.0.1:8771/',
  beforeParse(w) {
    w.HTMLElement.prototype.scrollIntoView = () => {};
    w.requestAnimationFrame = cb => setTimeout(() => cb(Date.now()), 0);
    w.echarts = {
      init(el) { return { setOption(o) { el.__opt = o; }, resize() {}, dispose() {}, getDom: () => el }; },
    };
    w.fetch = async (url, opt) => {
      const u = String(url);
      let payload = null;
      if (opt && opt.body) { try { payload = JSON.parse(opt.body); } catch (e) { /* 非 JSON */ } }
      reqs.push({ url: u, method: (opt && opt.method) || 'GET', payload });
      let body = {};
      if (u.includes('/api/meta')) body = FX.meta;
      else if (u.includes('/api/defaults')) body = FX.defaults;
      else if (u.includes('/api/market')) body = FX.market;
      else if (u.includes('/api/scan')) body = FX.scan;
      else if (u.includes('/api/backtest')) body = FX.backtest;
      else if (u.includes('/api/stock')) body = stockBody(payload);
      else if (u.includes('/api/trades')) body = { rows: [], n_trade: 0, summary: {}, plan: null };
      return {
        ok: true, status: 200, headers: { get: () => null },
        json: async () => body, text: async () => JSON.stringify(body),
        blob: async () => ({ size: 3 }), arrayBuffer: async () => new ArrayBuffer(3),
      };
    };
    w.URL.createObjectURL = () => 'blob:x';
    w.URL.revokeObjectURL = () => {};
    w.onerror = (m, s, l, c, e) => errors.push(`${m} @${l}:${c}`);
    w.console.error = (...a) => { errors.push(a.join(' ')); };
  },
});

const w = dom.window, d = w.document;
const $ = s => d.querySelector(s), $$ = s => [...d.querySelectorAll(s)];

const out = [];
const ok = (t, c, x) => out.push(`${c ? '✅' : '❌'} ${t}${x !== undefined ? '  → ' + x : ''}`);
const click = el => el.dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
const stkTxt = () => ($('#stk-out').textContent || '').replace(/\s+/g, ' ');

/* 该页最后一次 /api/stock 请求 */
const lastStock = () => [...reqs].reverse().find(r => r.url.includes('/api/stock'));

/* 查询一只票并等渲染完成 */
async function query(code) {
  $('#stk_code').value = code;
  click($('#stk_go'));
  for (let i = 0; i < 60; i++) {
    await new Promise(r => setTimeout(r, 10));
    if (lastStock() && $('#stk-out') && !$('#stk-out').querySelector('.spin')) return;
  }
}

/* 等某个条件成立（用于「点按钮 → 懒加载 → 重绘」这类异步） */
async function until(fn, ms = 900) {
  for (let i = 0; i < ms / 10; i++) {
    await new Promise(r => setTimeout(r, 10));
    if (fn()) return true;
  }
  return false;
}

/* 信号表里所有行的日期（按「日期」表头定位，不用列号） */
function tableDates() {
  const tbl = $('#stk-out table');
  if (!tbl) return [];
  const ths = $$('#stk-out th').map(x => x.textContent.replace(/[▲▼]/g, '').trim());
  const col = ths.indexOf('日期');
  if (col < 0) return [];
  return [...tbl.querySelectorAll('tbody tr')]
    .map(tr => tr.children[col].textContent.replace(/\s*图外\s*$/, '').trim());
}

setTimeout(async () => {
  // ---- 0. fixture 前提
  const S = FX.stock, SF = FX.stock_full;
  ok('fixture_stock 有行情行', Array.isArray(S.rows) && S.rows.length > 60,
     `${(S.rows || []).length} 行`);
  ok('fixture_stock 带 conditions 字段', Array.isArray(S.conditions), S.conditions);
  ok('fixture_stock 带 window 字段', typeof S.window === 'number', S.window);
  // 这是本次 bug 的事实前提：该股在「最小40% + 主板池」下确实有信号。
  // 若哪天数据变了，这条会失败 —— 那是提醒去复核，不是 bug。
  ok('fixture 前提：该股在宽松口径下确实有信号',
     (S.signal_dates || []).length > 0, `${(S.signal_dates || []).length} 个信号`);

  // ---- 1. 全历史契约（新）：signal_dates 是**全部历史**，且带 in_window 标记
  const sd = S.signal_dates || [];
  const nAll = S.n_signal_all, nWin = S.n_signal_window;
  ok('后端返回 n_signal_all / n_signal_window',
     typeof nAll === 'number' && typeof nWin === 'number', `${nAll} / ${nWin}`);
  ok('signal_dates 是**全历史**（条数 = n_signal_all，未被裁到窗口）',
     sd.length === nAll, `${sd.length} vs ${nAll}`);
  ok('每条信号都带 in_window 布尔标记',
     sd.length > 0 && sd.every(x => typeof x.in_window === 'boolean'));
  const winN = sd.filter(x => x.in_window).length;
  ok('in_window 计数与 n_signal_window 一致', winN === nWin, `${winN} vs ${nWin}`);
  // 这正是 Q2 的事实前提：有信号落在图表窗口**之外**（否则「看全历史」无从谈起）。
  ok('fixture 前提：存在窗口外的历史信号（Q2 的场景）',
     sd.length > winN, `窗外 ${sd.length - winN} 个`);
  ok('后端返回 hist_span（历史区间）',
     Array.isArray(S.hist_span) && S.hist_span.length === 2, S.hist_span && S.hist_span.join(' ~ '));
  // full_history 形态必须带 hist_rows + hist_sig_dates
  ok('fixture_stock_full 带 hist_rows（全历史行情）',
     Array.isArray(SF.hist_rows) && SF.hist_rows.length > S.rows.length,
     `${(SF.hist_rows || []).length} vs rows ${(S.rows || []).length}`);
  ok('fixture_stock_full 带 hist_sig_dates',
     Array.isArray(SF.hist_sig_dates) && SF.hist_sig_dates.length === nAll,
     (SF.hist_sig_dates || []).length);

  // ---- 2. 请求形态：POST + 带 params/pool
  await query('001256.SZ');
  const r1 = lastStock();
  ok('单股透视发起了 /api/stock 请求', !!r1);
  ok('用的是 POST（GET 无法携带 params/pool）', r1 && r1.method === 'POST', r1 && r1.method);
  ok('body 带 code', !!(r1 && r1.payload && r1.payload.code === '001256.SZ'),
     r1 && r1.payload && r1.payload.code);
  ok('body 带 params（跟随左侧参数）', !!(r1 && r1.payload && r1.payload.params));
  ok('body 带 pool（跟随左侧股票池）', !!(r1 && r1.payload && r1.payload.pool));

  /* ⚠️ 下面几条依赖 payload。若请求退化成 GET（payload 为 null）就**报名字失败**，
     而不是解引用崩溃 —— 崩溃会把后面所有断言一起吞掉（真实故障看不见）。 */
  const P1 = (r1 && r1.payload) || null;
  if (!P1) {
    ok('改市值区间后 size_max 跟着变（不是写死默认）', false, '请求无 body，无法验证');
    ok('改板块后 pool.boards 跟着变（不是写死不限池）', false, '请求无 body，无法验证');
  } else {
    // ---- 3. params 确实跟随左侧：改市值区间 → 发出去的 size_max 跟着变
    const before = P1.params && P1.params.size_max;
    const ev = (el, t) => el.dispatchEvent(new w.Event(t, { bubbles: true }));
    // 市值区间是**双把手 range 滑块**（不是按钮），用快捷档位按钮改最稳
    const quick = $$('#size_quick button');
    const nextMax = 9;   // 「全部」档 → size_max = 9，与默认 2 必然不同
    const qBtn = quick.find(b => Number(b.dataset.smax) === nextMax);
    if (qBtn) {
      click(qBtn);
    } else {
      const el = $('#size_max'); el.value = String(nextMax); ev(el, 'input');
    }
    await query('001256.SZ');
    const P2 = (lastStock() || {}).payload;
    ok('改市值区间后 size_max 跟着变（不是写死默认）',
       !!(P2 && P2.params) && Number(P2.params.size_max) !== Number(before),
       `before=${before} → after=${P2 && P2.params && P2.params.size_max}`);

    // ---- 4. pool 确实跟随左侧：关掉一个板块 → boards 跟着少一个
    const bdBtn = $$('#seg-board button').find(b => b.classList.contains('on'));
    const nBdBefore = (P2 && P2.pool && P2.pool.boards || []).length;
    if (bdBtn && nBdBefore > 0) {
      click(bdBtn);
      await query('001256.SZ');
      const P3 = (lastStock() || {}).payload;
      const nBdAfter = (P3 && P3.pool && P3.pool.boards || []).length;
      ok('改板块后 pool.boards 跟着变（不是写死不限池）',
         nBdAfter === nBdBefore - 1, `${nBdBefore} → ${nBdAfter}`);
      click(bdBtn);   // 还原
    } else {
      ok('改板块后 pool.boards 跟着变（不是写死不限池）', false,
         `未找到板块按钮或池为空（before=${nBdBefore}）`);
    }
  }

  // ---- 5. 渲染「本页所用条件」
  await query('001256.SZ');
  const t = stkTxt();
  ok('渲染了「本页所用条件」区块', /本页所用条件/.test(t));
  ok('条件文案来自后端 conditions（与主面板同源）',
     (FX.stock.conditions || []).every(c => t.includes(c)),
     (FX.stock.conditions || []).join(' / '));
  const pills = $$('#stk-out .pill').map(p => p.textContent.trim());
  ok('条件用徽标渲染', pills.length === (FX.stock.conditions || []).length,
     `${pills.length} 个`);
  ok('显示了股票池摘要', /股票池/.test(t));

  // ---- 6. 信号明细表：**列全历史**
  const tbl = $('#stk-out table');
  ok('渲染了历史信号明细表', !!tbl);
  if (tbl) {
    const ths = $$('#stk-out th').map(x => x.textContent.replace(/[▲▼]/g, '').trim());
    ok('信号表按表头名可定位（不用列号）', ths.includes('日期') && ths.includes('距MA60'), ths.join('|'));
    const rowsN = [...tbl.querySelectorAll('tbody tr')].length;
    ok('信号表行数 = signal_dates 全历史条数（**没有**被裁到窗口）',
       rowsN === sd.length, `${rowsN} vs ${sd.length}`);
    // 表里日期集合 == 后端给的信号日期集合（一个不多、一个不少）
    const shown = tableDates().slice().sort();
    const expect = sd.map(x => x.date).slice().sort();
    ok('信号表覆盖后端返回的**每一个**信号日',
       shown.length === expect.length && shown.every((x, i) => x === expect[i]),
       shown.length ? `表 ${shown.length} 个 / 期望 ${expect.length} 个` : '表为空');
    // 用户要的：全历史能看见 —— 特别是**窗口外**那几个
    const outDates = sd.filter(x => !x.in_window).map(x => x.date);
    ok('窗口外的历史信号也**出现在表里**（用户诉求：看到所有历史）',
       outDates.every(dt => shown.includes(dt)),
       outDates.length ? `窗外 ${outDates.length} 个：${outDates.join(',')}` : '无窗外信号');
    // 且被明确标注（标灰 + 「图外」），不是只靠颜色
    const outRows = [...tbl.querySelectorAll('tbody tr')].filter(tr => tr.classList.contains('stockrow-out'));
    ok('窗口外的行带 stockrow-out 类（标灰）', outRows.length === outDates.length,
       `${outRows.length} vs ${outDates.length}`);
    const marked = outRows.filter(tr => /图外/.test(tr.textContent)).length;
    ok('窗口外的行注明「图外」（不隐藏、可解释）', marked === outRows.length,
       `${marked}/${outRows.length}`);
    // 表标题要说明这是全历史
    ok('信号表标题标注「全历史」', /全历史/.test(t));
    ok('表头标出全历史次数（= n_signal_all）',
       new RegExp(`全历史\\s*${nAll}\\s*次`).test(t) || t.includes(`全历史 ${nAll} 次`),
       `期望 ${nAll} 次`);
  }
  // ---- 6b. 未走完的持有期必须说明（否则「—」看起来像算错了）
  const pendN = sd.filter(s => s.fwd20 === null).length;
  if (pendN > 0) {
    ok('未走完持有期的信号有说明（不是静默显示「—」）', /尚未走完/.test(t),
       `pending=${pendN}`);
  }
  // ---- 6c. 不能再说「默认参数下触发」（本页已跟随左侧参数，文案会误导）
  ok('信号表标题不再写「默认参数下触发」', !/默认参数下触发/.test(t));
  // 旧文案「仅近 N 交易日」把表也说成窗口内的 —— 与新契约矛盾，必须消失
  ok('不再声称信号表「仅近 N 交易日」（表是全历史）', !/仅近\s*250\s*交易日/.test(t));

  // ---- 7. 「全部历史」图表切换
  const fb = $('#stk_full');
  ok('存在「全部历史」切换按钮', !!fb, fb && fb.textContent.trim());
  if (fb) {
    ok('首次进入时图表是窗口视图（按钮写「全部历史」）',
       /全部历史/.test(fb.textContent), fb.textContent.trim());
    const nReqBefore = reqs.filter(r => r.url.includes('/api/stock')).length;
    click(fb);
    const gotFull = await until(() => $('#stk_full') && /近\s*250\s*日/.test($('#stk_full').textContent));
    ok('点「全部历史」后按钮变为「近 250 日」（即已切到全历史视图）', gotFull,
       $('#stk_full') && $('#stk_full').textContent.trim());
    // 懒加载：应发出一次 full_history=true 的请求
    const rFull = [...reqs].reverse().find(r => r.payload && r.payload.full_history === true);
    ok('全历史视图按需懒加载（发出 full_history=true 的 POST）', !!rFull,
       rFull ? 'ok' : `请求数 ${nReqBefore} → ${reqs.filter(r => r.url.includes('/api/stock')).length}`);
    // 图表数据源切成 hist_rows：行数应等于 hist_rows 长度
    const opt = $('#ch-stk') && $('#ch-stk').__opt;
    const nPts = opt && opt.xAxis && opt.xAxis.data ? opt.xAxis.data.length : 0;
    ok('全历史视图的图表点数 = hist_rows 长度',
       nPts === (SF.hist_rows || []).length, `${nPts} vs ${(SF.hist_rows || []).length}`);
    ok('全历史视图图表含「信号」散点系列',
       !!(opt && (opt.series || []).some(s => s.name === '信号')));
    // 切回窗口
    click($('#stk_full'));
    await until(() => $('#stk_full') && /全部历史/.test($('#stk_full').textContent));
    const opt2 = $('#ch-stk') && $('#ch-stk').__opt;
    const nPts2 = opt2 && opt2.xAxis && opt2.xAxis.data ? opt2.xAxis.data.length : 0;
    ok('可切回窗口视图（点数回到 rows 长度 = 250）', nPts2 === (S.rows || []).length,
       `${nPts2} vs ${(S.rows || []).length}`);
  }

  // ---- 8. 年份筛选（纯前端，只影响显示）
  const yf = $('#stk_yf');
  ok('提供年份/日期筛选输入框', !!yf);
  if (yf) {
    /* ⚠️ 必须**重新查询**表格：上面「全部历史」切换会重绘 `#stk-out`，
       早先捕获的 `tbl` 已是脱离文档的旧节点（对它读 style.display 永远看到原值）。 */
    const tb2 = $('#stk_tb');
    ok('筛选用的表格 tbody 存在', !!tb2);
    if (tb2) {
      const yr = String(sd[0].year);
      yf.value = yr;
      yf.dispatchEvent(new w.Event('input', { bubbles: true }));
      const vis = [...tb2.querySelectorAll('tr')].filter(tr => tr.style.display !== 'none');
      const expN = sd.filter(x => String(x.year) === yr).length;
      ok(`筛选「${yr}」后只显示该年度的信号`, vis.length === expN, `${vis.length} vs ${expN}`);
      ok('筛选后给出命中条数提示', new RegExp(`筛选「${yr}」`).test(stkTxt()));
      const clr = $('#stk_yfclr');
      if (clr) {
        click(clr);
        const all = [...tb2.querySelectorAll('tr')].filter(tr => tr.style.display !== 'none');
        ok('「清除」后恢复显示全部行', all.length === sd.length, `${all.length} vs ${sd.length}`);
      } else {
        ok('「清除」按钮存在', false, '未找到 #stk_yfclr');
      }
    }
  }

  // ---- 9. 表/图一致性说明（窗口外信号必须被点明，而不是让人以为是 bug）
  ok('页面解释了「有多少信号不在当前图上」',
     /个信号不在/.test(stkTxt()) || /全部落在当前范围内/.test(stkTxt()));

  // ---- 10. 无运行时错误
  ok('无运行时错误', errors.length === 0, errors.slice(0, 3).join(' | '));

  console.log(out.join('\n'));
  const pass = out.filter(x => x.startsWith('✅')).length;
  console.log(`\n通过 ${pass}/${out.length}`);
  process.exit(pass === out.length ? 0 : 1);
}, 60);
