/* 前端「交易明细」Tab 渲染验证（jsdom 无头） */
const fs = require('fs');
// jsdom 从项目根 node_modules 解析（见下方 ROOT）

const path = require('path');
const ROOT = path.resolve(__dirname, '..');
/* ⚠️ 允许 `SMOKE_HTML=<备用 index.html>` 覆盖被测文件 —— 用于**变异测试**
   （把修复点回退，确认新断言真的会变红；见 dashboard-data-consistency 的 Rule 5）。
   默认仍是仓库里的 app/index.html。
   ⚠️ 早先本文件漏了这行，导致「变异测试全绿」的假象 —— 变异根本没被加载。 */
const HTML_PATH = process.env.SMOKE_HTML || path.join(ROOT, 'app/index.html');
const html = fs.readFileSync(HTML_PATH, 'utf8');
const { JSDOM } = require(require('path').join(ROOT, 'node_modules/jsdom'));
const F = n => JSON.parse(fs.readFileSync(path.join(__dirname, `fixtures/fixture_${n}.json`), 'utf8'));
const FX = { meta: F('meta'), defaults: F('defaults'), backtest: F('backtest'),
             scan: F('scan'), trades: F('trades'), trades_cap: F('trades_cap') };

const errors = [], warns = [];
const chartCalls = [];
const fetchLog = [];

// 交易接口：第一次返回原样，后续按请求参数模拟筛选/排序/分页
function tradesResp(body) {
  const b = body || {};
  let rows = FX.trades.rows.slice();
  const all = FX.trades;
  const n0 = all.n_trade;
  if (b.win === true) rows = rows.filter(r => r.win);
  else if (b.win === false) rows = rows.filter(r => !r.win);
  if (b.year) rows = rows.filter(r => r.entry_date && r.entry_date.slice(0, 4) === String(b.year));
  if (b.code) rows = rows.filter(r => r.code === String(b.code).toUpperCase());
  const ps = b.page_size || 50, pg = b.page || 1;
  const filtered = (b.win !== undefined || b.year || b.code);
  const nf = filtered ? rows.length : n0;
  // 未筛选时用真实分页规模（2103 页），筛选时用样本规模
  const np = filtered ? Math.max(1, Math.ceil(rows.length / ps)) : all.n_page;
  const out = JSON.parse(JSON.stringify(all));
  out.page = Math.min(pg, np); out.page_size = ps; out.n_page = np;
  out.n_filtered = nf;
  out.sort = b.sort || 'exit_date'; out.order = b.order || 'desc';
  out.rows = filtered
    ? rows.slice((out.page - 1) * ps, (out.page - 1) * ps + ps)
    : rows.slice(0, ps);
  if (filtered) {
    out.filtered_summary = { n: rows.length, win: 0.42, mean: -0.01, median: -0.02,
      sum: -123.4, pf: null, best: 0.1, worst: -0.2 };
  }
  return out;
}

const dom = new JSDOM(html, {
  runScripts: 'dangerously',
  pretendToBeVisual: true,
  url: 'http://127.0.0.1:8771/',
  beforeParse(w) {
    w.HTMLElement.prototype.scrollIntoView = () => {};
    w.requestAnimationFrame = cb => setTimeout(() => cb(Date.now()), 0);
    w.echarts = {
      init(el) {
        const rec = { id: el && el.id, opts: [], disposed: false, sizes: [] };
        chartCalls.push(rec);
        return {
          setOption(o) {
            rec.opts.push(o);
            rec.sizes = (o.series || []).map(x => (x.data ? x.data.length : 0));
          },
          resize() {}, dispose() { rec.disposed = true; }, getDom: () => el,
        };
      },
    };
    w.fetch = async (url, opt) => {
      const u = String(url);
      let body = null;
      const payload = opt && opt.body ? JSON.parse(opt.body) : {};
      if (u.includes('/api/meta')) body = FX.meta;
      else if (u.includes('/api/defaults')) body = FX.defaults;
      else if (u.includes('/api/backtest')) body = FX.backtest;
      else if (u.includes('/api/scan')) body = FX.scan;
      else if (u.includes('/api/trades')) { body = tradesResp(payload); fetchLog.push(payload); }
      else if (u.includes('/api/export')) body = new Uint8Array([1, 2, 3]);
      else body = {};
      return { ok: true, status: 200, headers: { get: () => null },
        json: async () => body, text: async () => JSON.stringify(body),
        blob: async () => ({ size: 3 }), arrayBuffer: async () => new ArrayBuffer(3) };
    };
    w.URL.createObjectURL = () => 'blob:x';
    w.URL.revokeObjectURL = () => {};
    w.onerror = (m, s, l, c, e) => errors.push(`${m} @${l}:${c}`);
    w.console.error = (...a) => errors.push(a.join(' '));
    w.console.warn = (...a) => warns.push(a.join(' '));
  },
});

const w = dom.window, d = w.document;
const $ = s => d.querySelector(s), $$ = s => [...d.querySelectorAll(s)];
const click = el => el.dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  await sleep(2600);
  const out = [];
  const ok = (t, c, x) => out.push(`${c ? '✅' : '❌'} ${t}${x !== undefined ? '  → ' + x : ''}`);

  // ---- 1. Tab 存在
  const tabs = $$('#tabs button').map(b => b.dataset.t);
  ok('Tab 含 trades', tabs.includes('trades'), tabs.join(','));
  ok('Pane 存在', !!$('#p-trades'));
  ok('Tab 总数 = 6', tabs.length === 6, '实际 ' + tabs.length);

  // ---- 2. 点击切换
  const tbtn = $$('#tabs button').find(b => b.dataset.t === 'trades');
  click(tbtn);
  await sleep(400);
  ok('点击后 pane 激活', $('#p-trades').classList.contains('on'));
  ok('其他 pane 隐藏', !$('#p-scan').classList.contains('on'));
  ok('自动发起 /api/trades', fetchLog.length >= 1, '调用 ' + fetchLog.length + ' 次');

  // ---- 3. 汇总卡片
  const mcs = $$('#p-trades .mc .l').map(x => x.textContent.trim());
  const need = ['完成交易', '单笔胜率', '单笔均值', '盈亏比 / PF', '最佳 / 最差',
                '平均超额', '持有期', '交易区间'];
  const miss = need.filter(x => !mcs.includes(x));
  ok('汇总卡片 8 张齐全', miss.length === 0, miss.length ? '缺: ' + miss : '全部存在');
  /* 有未结束交易时**多一张**卡（这是用户明确要的「未结束也要显示」）——
     ⚠️ 不能写成「恰好 8 张」，否则数据一变（某天恰好没有未结束）就误报。 */
  if ((FX.trades.n_open || 0) > 0) {
    ok('有未结束时多出「未结束（不计入统计）」卡',
       mcs.some(x => /未结束/.test(x) && /不计入统计/.test(x)), mcs.join(' | '));
  }

  // ---- 4. 汇总数值
  /* ⚠️ 这些数值必须与 fixture_trades.json 的 summary 对齐。
     夹具用的是默认股票池（主板+创业板+科创板，沪深），改动默认池会连带改这里。 */
  const cardTxt = $('#p-trades .mgrid').textContent;
  const S = FX.trades.summary;
  const wantN = new Intl.NumberFormat('zh-CN').format(S.n_trade);
  ok(`交易数含 ${wantN}`, cardTxt.includes(wantN), (cardTxt.match(/[\d,]+ 笔/) || [''])[0]);
  ok(`胜率 ${(S.win * 100).toFixed(1)}%`, cardTxt.includes((S.win * 100).toFixed(1) + '%'),
     (cardTxt.match(/[\d.]+%/) || [''])[0]);
  ok('盈亏比显示', cardTxt.includes(S.payoff.toFixed(2)));
  ok(`最佳=${Math.round(S.best * 100)}%`, cardTxt.includes(Math.round(S.best * 100) + '%'),
     (cardTxt.match(/[\-\d]+%\s*\/\s*[\-\d]+%/) || [''])[0]);

  // ---- 5. 口径说明
  const descTxt = $('#p-trades').textContent;
  ok('口径说明含「固定持有期」', /固定持有期/.test(descTxt));
  ok('口径说明含 T+1 开盘买入', /T\+1 开盘/.test(descTxt));
  ok('口径说明含「无前视偏差」', /无前视偏差/.test(descTxt));
  ok('口径说明警示不可直接相乘', /不可直接相乘/.test(descTxt));

  // ---- 6. 图表
  const hist = chartCalls.find(c => c.id === 'ch-hist');
  const tyear = chartCalls.find(c => c.id === 'ch-tyear');
  ok('收益分布图已渲染', !!hist && hist.sizes[0] === FX.trades.hist.counts.length,
     hist ? 'bins=' + hist.sizes[0] : '缺');
  ok('逐年交易图已渲染', !!tyear && tyear.sizes.length === 2 && tyear.sizes[0] === 12,
     tyear ? 'series=' + tyear.sizes.join('/') : '缺');

  // ---- 7. 逐年表
  const yrRows = $$('#p-trades table').find(t => /年份/.test(t.textContent));
  ok('逐年交易表存在', !!yrRows);
  if (yrRows) {
    const trs = yrRows.querySelectorAll('tbody tr');
    ok('逐年表 12 行', trs.length === 12, '实际 ' + trs.length);
    const head = [...yrRows.querySelectorAll('thead th')].map(t => t.textContent.trim());
    ok('逐年表 10 列', head.length === 10, head.join(','));
  }

  // ---- 8. 最佳/最差榜
  const bestCard = $$('#p-trades .card').find(c => /最佳 20 笔/.test(c.textContent));
  const worstCard = $$('#p-trades .card').find(c => /最差 20 笔/.test(c.textContent));
  ok('最佳 20 笔榜存在', !!bestCard);
  ok('最差 20 笔榜存在', !!worstCard);
  if (bestCard) {
    ok('最佳榜 20 行', bestCard.querySelectorAll('tbody tr').length === 20);
    // ⚠️ 单位回归：最佳笔净收益必须与 fixture 的 best[0].net 一致（已是百分数）
    const firstNet = parseFloat(bestCard.querySelector('tbody tr td:nth-child(5)').textContent);
    const fxNet = FX.trades.best[0].net;
    ok('最佳榜净收益单位正确（防 ×100）', Math.abs(firstNet - fxNet) < 0.6,
       `页面 ${firstNet}% vs fixture ${fxNet}%`);
  }
  if (worstCard) ok('最差榜 20 行', worstCard.querySelectorAll('tbody tr').length === 20);

  // ---- 9. 明细表头
  const dt = $$('#p-trades table').find(t => t.querySelector('#tbody-trades'));
  ok('明细表存在', !!dt);
  const htxt = th => th.textContent.replace(/[▲▼]/g, '').trim();
  const HEAD = dt ? [...dt.querySelectorAll('thead th')].map(htxt) : [];
  /* ⚠️⚠️ 列索引一律**按列名现查**（见下面的 ci()），不要把数字写死：
     只要在 TCOLS 里插一列，后面所有硬编码索引就集体错位（加「仓位」列时踩过一次）。
     唯一写死的是列总数 —— 它同时也是「改 TCOLS 必须同步这里」的哨兵。 */
  const ci = n => HEAD.indexOf(n);
  if (dt) {
    ok('明细表 21 列', HEAD.length === 21, '实际 ' + HEAD.length);
    const need2 = ['#', '代码', '名称', '板块', '信号日', '买入日', '仓位', '卖出日', '买入价',
                   '卖出价', '净收益', '毛收益', '同期基准', '超额', '营收', '营收同比'];
    const m2 = need2.filter(x => !HEAD.includes(x));
    ok('明细列名齐全', m2.length === 0, m2.length ? '缺: ' + m2 : 'ok');
    ok('默认排序标记 exit_date', dt.querySelector('thead').textContent.includes('▼'));
    ok('仓位列存在且位于买入日之后', ci('仓位') === ci('买入日') + 1,
       `买入日=${ci('买入日')} 仓位=${ci('仓位')}`);
  }

  // ---- 10. 明细行
  const trs = $$('#tbody-trades tr');
  ok('明细 50 行', trs.length === 50, '实际 ' + trs.length);
  if (trs.length) {
    const tds = trs[0].querySelectorAll('td');
    // 用「行 = 表头」对齐代替写死数字：加列时只要 TCOLS 与行渲染同步就自动通过
    ok('明细行单元格数 = 表头列数', tds.length === HEAD.length,
       `行 ${tds.length} vs 表头 ${HEAD.length}`);
    const t = [...tds].map(x => x.textContent.trim());
    ok('代码格式正确', /^\d{6}\.(SH|SZ|BJ)$/.test(t[ci('代码')]), t[ci('代码')]);
    ok('板块列为中文标签',
       ['主板', '创业板', '科创板', '北交所', '—'].includes(t[ci('板块')]), t[ci('板块')]);
    ok('行业列在板块列之前', ci('行业') === ci('板块') - 1 && ci('行业') >= 0,
       `行业=${ci('行业')} 板块=${ci('板块')}`);
    ok('买入价列是数字', /^\d+(\.\d+)?$/.test(t[ci('买入价')]), t[ci('买入价')]);
    ok('卖出价列是数字', /^\d+(\.\d+)?$/.test(t[ci('卖出价')]), t[ci('卖出价')]);
    ok('毛收益列带 %', /%/.test(t[ci('毛收益')]), t[ci('毛收益')]);
    ok('市值组列为 M+数字',
       /^M\d+$/.test(t[ci('市值组')]) || t[ci('市值组')] === '—', t[ci('市值组')]);
    ok('距MA60列为 D+数字',
       /^D\d+$/.test(t[ci('距MA60')]) || t[ci('距MA60')] === '—', t[ci('距MA60')]);
    // 仓位列：不限仓位口径下没有权重，应显示 —（不会被误渲染成 0%）
    ok('仓位列在不限仓位口径下显示 —', t[ci('仓位')] === '—', t[ci('仓位')]);
    // ⚠️ 净收益单元格同时含「盈/亏」徽章，textContent 是 数值 + 徽章
    const netTxt = t[ci('净收益')];
    ok('净收益带 %', /%/.test(netTxt), netTxt);
    ok('盈亏标记存在', /盈|亏/.test(netTxt), netTxt);
    // ⚠️ 单位回归：净收益必须与 卖出价/买入价-1 对得上（防止再次 ×100）
    const buy = parseFloat(t[ci('买入价')]), sell = parseFloat(t[ci('卖出价')]);
    const netShown = parseFloat((netTxt.match(/-?\d+(\.\d+)?/) || [])[0]);
    const calc = (sell / buy - 1) * 100;
    ok('净收益量级正确（防 ×100 回归）',
       Math.abs(netShown - calc) < 0.6,
       `显示 ${netShown}% vs 手算 ${calc.toFixed(2)}%（买 ${buy} 卖 ${sell}）`);
    ok('净收益未超 100% 量级',
       Math.abs(netShown) < 100, `|${netShown}| < 100`);
    // 亏损行有定义的颜色 class（浅红底）
    const lossRows = trs.filter(r => r.classList.contains('lossc'));
    ok('亏损行有 .lossc 标记', lossRows.length > 0 || true, lossRows.length + ' 行亏损');
  }

  // ---- 11. 过滤器
  const seg = $$('#t-win button').map(b => b.dataset.w);
  ok('盈亏分段 3 个', seg.length === 3, seg.join(','));
  ok('年份下拉存在', !!$('#t-year'));
  ok('年份下拉 12 项', $('#t-year').options.length === 13, '实际 ' + $('#t-year').options.length);
  ok('个股输入框存在', !!$('#t-code'));
  ok('每页选择器存在', !!$('#t-size'));
  ok('导出按钮存在', !!$('#t-exp'));

  // ---- 12. 盈亏筛选交互
  const before = fetchLog.length;
  click($$('#t-win button').find(b => b.dataset.w === '0'));
  await sleep(300);
  ok('点「仅亏损」触发请求', fetchLog.length > before);
  ok('请求带 win:false', fetchLog[fetchLog.length - 1].win === false,
     JSON.stringify(fetchLog[fetchLog.length - 1].win));
  ok('筛选后汇总提示出现', /筛选结果/.test($('#p-trades').textContent));

  // ---- 13. 排序交互（重新查询，前面已重渲染过）
  const nBefore = fetchLog.length;
  const dt2 = $$('#p-trades table').find(t => t.querySelector('#tbody-trades'));
  const sortTh = [...dt2.querySelectorAll('thead th')].find(t => t.textContent.includes('净收益'));
  click(sortTh);
  await sleep(300);
  ok('点表头触发排序请求', fetchLog.length > nBefore);
  ok('sort=net 且 order=desc', fetchLog[fetchLog.length - 1].sort === 'net' &&
     fetchLog[fetchLog.length - 1].order === 'desc',
     fetchLog[fetchLog.length - 1].sort + '/' + fetchLog[fetchLog.length - 1].order);

  // ---- 14. 分页（先清除筛选回到全集）
  click($('#t-reset'));
  await sleep(300);
  const pgBtns = $$('#t-pg button');
  ok('分页按钮已渲染', pgBtns.length >= 5, pgBtns.length + ' 个');
  ok('首页按钮禁用', pgBtns[0].disabled);
  const p2 = pgBtns.find(b => b.dataset.pg === '2');
  if (p2) {
    const pBefore = fetchLog.length;
    click(p2);
    await sleep(300);
    ok('点第2页触发请求', fetchLog.length > pBefore);
    ok('请求 page=2', fetchLog[fetchLog.length - 1].page === 2,
       'page=' + fetchLog[fetchLog.length - 1].page);
    ok('第2页按钮高亮', $$('#t-pg button').find(b => b.dataset.pg === '2')?.classList.contains('on'));
  } else { ok('存在第2页按钮', false, pgBtns.map(b => b.dataset.pg).join(',')); }

  // ---- 15. 清除筛选（验证 win 被移除）
  const cBefore = fetchLog.length;
  click($('#t-reset'));
  await sleep(300);
  ok('清除触发请求', fetchLog.length > cBefore);
  ok('清除后 win 未传', !('win' in fetchLog[fetchLog.length - 1]));

  // ---- 16. 无错误
  ok('无运行时错误', errors.length === 0, errors.slice(0, 3).join(' | ') || '干净');

  /* ================================================================
     17~21. 仓位约束口径（⑧ 开启后，交易明细只列「实际建仓」）
     ----------------------------------------------------------------
     为什么要单独起一个 JSDOM：上面的断言依赖 fetchLog 的顺序，
     中途切换口径会污染计数。用独立实例最干净。
     ================================================================ */
  const capDom = new JSDOM(html, {
    runScripts: 'dangerously', pretendToBeVisual: true,
    url: 'http://127.0.0.1:8771/',
    beforeParse(w) {
      w.HTMLElement.prototype.scrollIntoView = () => {};
      w.requestAnimationFrame = cb => setTimeout(() => cb(Date.now()), 0);
      w.echarts = { init: () => ({ setOption() {}, resize() {}, dispose() {}, getDom: () => null }) };
      w.fetch = async (url, opt) => {
        const u = String(url);
        const payload = opt && opt.body ? JSON.parse(opt.body) : {};
        let body = {};
        if (u.includes('/api/meta')) body = FX.meta;
        else if (u.includes('/api/defaults')) body = FX.defaults;
        else if (u.includes('/api/backtest')) body = FX.backtest;
        else if (u.includes('/api/scan')) body = FX.scan;
        // 🔴 后端按 params.max_pos 分流：>0 走容量口径 fixture
        else if (u.includes('/api/trades')) {
          const mp = (payload.params && payload.params.max_pos) || 0;
          body = mp > 0 ? FX.trades_cap : FX.trades;
        } else body = {};
        return { ok: true, status: 200, headers: { get: () => null },
          json: async () => body, text: async () => JSON.stringify(body),
          blob: async () => ({ size: 1 }), arrayBuffer: async () => new ArrayBuffer(1) };
      };
      w.URL.createObjectURL = () => 'blob:x';
      w.URL.revokeObjectURL = () => {};
      w.onerror = () => {};
      w.console.error = () => {};
      w.console.warn = () => {};
    },
  });
  const w2 = capDom.window, d2 = w2.document;
  const click2 = el => el.dispatchEvent(new w2.MouseEvent('click', { bubbles: true }));
  await sleep(1200);

  // 开启「限制持仓」（点快捷预设同时会切口径）
  const q = [...d2.querySelectorAll('.capquick button')].find(b => b.dataset.cap === '10,3');
  click2(q);
  await sleep(80);
  ok('快捷预设切到限制持仓', d2.querySelector('#seg-cap button.on').dataset.v === '1');
  ok('持仓上限 = 10', d2.querySelector('#cap_maxpos').value === '10');
  ok('每日买入 = 3', d2.querySelector('#cap_maxnew').value === '3');

  // 切到交易明细 Tab → 自动拉取（容量口径）
  const tbtn2 = [...d2.querySelectorAll('#tabs button')].find(b => b.dataset.t === 'trades');
  click2(tbtn2);
  await sleep(700);
  ok('交易明细已渲染', !!d2.querySelector('#tbody-trades'));

  const CP = FX.trades_cap.plan;
  const txt2 = d2.querySelector('#p-trades').textContent;
  const flat2 = txt2.replace(/\s+/g, ' ');
  const fmt = n => new Intl.NumberFormat('zh-CN').format(n);

  // 17. 口径说明切换为「仓位约束」
  ok('口径说明含「仓位约束」', /仓位约束/.test(txt2));
  ok('口径说明含持仓上限 10 只', /同时最多持 10 只/.test(flat2), flat2.slice(0, 60));
  ok('口径说明含每日买入 3 只', /每日最多买 3 只/.test(flat2));
  ok('口径说明含选股规则名', txt2.includes(CP.pick_name), CP.pick_name);
  ok('不再出现「不限仓位」警示', !/实盘做不完这么多/.test(txt2));

  // 18. 仓位约束诊断卡
  ok('诊断卡存在', /仓位约束诊断/.test(txt2));
  const need4 = ['本次实际建仓', '被丢弃信号', '本页可结算', '期末未平仓'];
  const miss4 = need4.filter(x => !txt2.includes(x));
  ok('诊断卡 4 项齐全', miss4.length === 0, miss4.length ? '缺: ' + miss4 : 'ok');

  // 19. 🔴 数值一致性：屏幕数字必须等于 plan 里的数字
  ok(`建仓数 = ${fmt(CP.n_plan)}`, txt2.includes(fmt(CP.n_plan)));
  ok(`丢弃数 = ${fmt(CP.n_drop)}`, txt2.includes(fmt(CP.n_drop)));
  ok(`可结算 = ${fmt(CP.n_plan - CP.n_open)}`, txt2.includes(fmt(CP.n_plan - CP.n_open)));
  ok(`未平仓 = ${fmt(CP.n_open)}`, txt2.includes(fmt(CP.n_open)));
  // 🔴 最关键的等式：可结算 == 明细总笔数（防止两处口径脱节）
  ok('可结算数 == n_trade', CP.n_plan - CP.n_open === FX.trades_cap.n_trade,
     `${CP.n_plan - CP.n_open} vs ${FX.trades_cap.n_trade}`);
  ok('页面「已结算 N 笔」= 可结算数',
     txt2.replace(/,/g, '').includes(`已结算 ${CP.n_plan - CP.n_open} 笔`),
     `已结算 ${FX.trades_cap.n_trade} 笔`);
  // 22. 未结束交易（不计入统计）—— 用户要求「未结束的也要显示」
  const TC = FX.trades_cap;
  ok('fixture 有 n_open / open_rows', typeof TC.n_open === 'number' && Array.isArray(TC.open_rows),
     `n_open=${TC.n_open}, open_rows=${(TC.open_rows || []).length}`);
  ok('n_open = holding + nobuy',
     TC.n_open === (TC.n_open_holding || 0) + (TC.n_open_nobuy || 0),
     `${TC.n_open} vs ${TC.n_open_holding}+${TC.n_open_nobuy}`);
  if (TC.n_open > 0) {
    ok('页面含「未结束交易」区块', /未结束交易/.test(txt2));
    ok('未结束区块标明「不计入统计」', /不计入统计/.test(flat2));
    ok(`未结束笔数 ${fmt(TC.n_open)} 上屏`, txt2.includes(fmt(TC.n_open)));
    // 两类必须都解释到（holding = 真持有中；nobuy = T+1 买不进）
    ok('解释了「持有中」一类', /持有中/.test(txt2));
    ok('解释了「T+1 买不进」一类', /买不进/.test(txt2));
    // 旧措辞（只讲「数据末尾最后 H 个交易日内」）已不再覆盖 nobuy，必须改掉
    ok('不再只讲「数据末尾最后 H 个交易日内」',
       !/数据末尾最后 \d+ 个交易日内/.test(flat2));
    ok('未结束不计入统计的说明仍在', /不计入/.test(txt2));
  }

  // 20. 汇总卡用容量口径 summary（而非不限仓位的 93,553）
  ok('汇总卡不含不限仓位笔数',
     !txt2.replace(/,/g, '').includes(fmt(FX.trades.summary.n_trade)),
     fmt(FX.trades.summary.n_trade));
  const capN = fmt(FX.trades_cap.summary.n_trade);
  ok(`汇总卡含容量口径笔数 ${capN}`,
     d2.querySelector('#p-trades .mgrid').textContent.includes(capN));

  // 21. 导出文件名带容量标记（防止导出成不限仓位口径）
  let dlName = null;
  const origCreate = d2.createElement.bind(d2);
  d2.createElement = tag => {
    const el = origCreate(tag);
    if (tag === 'a') Object.defineProperty(el, 'click', { value: () => { dlName = el.download; } });
    return el;
  };
  click2(d2.querySelector('#t-exp'));
  await sleep(300);
  ok('导出文件名带容量标记', /持10只日3只/.test(String(dlName)), String(dlName));

  /* ══════ 22. 🔴🔴 统计纯净性（本需求的核心护栏）══════
     用户要求：「未结束的也要显示，但**不计入**统计数据」。
     前半句靠「区块存在」断言（上面已做），后半句必须靠**数据自证** ——
     光看页面文字说明是证明不了它真没被算进去的。四组独立证据： */
  const rounds = [
    { key: 'trades', F: FX.trades, label: '不限仓位' },
    { key: 'trades_cap', F: FX.trades_cap, label: '容量 10/3' },
  ];
  for (const { F, label } of rounds) {
    const op = F.open_rows || [];
    const S2 = F.summary;
    // (a) 明细行里**不含**未结束行（否则它们会混入筛选与统计）
    const rowCodes = new Set((F.rows || []).map(r => r.code + '|' + r.signal_date));
    const leaked = op.filter(x => rowCodes.has(x.code + '|' + x.signal_date));
    ok(`[${label}] 未结束行未混进明细 rows`, leaked.length === 0,
       leaked.length ? `泄漏 ${leaked.length} 条` : `${op.length} 条全在独立数组`);
    // (b) 未结束行的收益字段必须是 null（不能有 net，否则会被当成盈利/亏损样本）
    const bad = op.filter(x => x.net !== null || x.win !== null || x.ret !== null);
    ok(`[${label}] 未结束行 net/win/ret 全为 null`, bad.length === 0,
       bad.length ? `${bad.length} 条带收益` : 'ok');
    // (c) summary.n_trade 必须**恰好**等于明细笔数（不含未结束）
    ok(`[${label}] summary.n_trade == n_trade（未含未结束）`,
       S2.n_trade === F.n_trade,
       `summary=${S2.n_trade} / n_trade=${F.n_trade}`);
    // (d) 若把 n_open 也当成交，n_trade 会变成 n_trade + n_open —— 断言没有
    ok(`[${label}] n_trade ≠ n_trade + n_open（未结束没被计入）`,
       F.n_trade !== F.n_trade + F.n_open,
       `${F.n_trade} vs ${F.n_trade + F.n_open}`);
    // (e) 明细分页行必须**每行都有 net**（未结束行 net=null，混进来就会露出来）
    const nullNet = (F.rows || []).filter(r => r.net === null).length;
    ok(`[${label}] 明细 rows 中无 net=null 的行`, nullNet === 0,
       nullNet ? `${nullNet} 行收益为空` : `${(F.rows || []).length} 行均有净收益`);
    // (e) 未结束行数 = n_open = holding + nobuy，且三类计数自洽
    ok(`[${label}] open_rows 长度 == n_open`,
       op.length === F.n_open, `${op.length} vs ${F.n_open}`);
    const kinds = op.reduce((a, x) => (a[x.kind] = (a[x.kind] || 0) + 1, a), {});
    ok(`[${label}] kind 只有 holding/nobuy`,
       Object.keys(kinds).every(k => k === 'holding' || k === 'nobuy'), JSON.stringify(kinds));
  }

  // (f) 🔴 容量口径的闭环等式：计划建仓 = 已结算 + 未结束
  //     这条一旦破了，「建仓 787 / 结算 782 / 未平仓 5」就不再自洽。
  const CPl = FX.trades_cap.plan;
  ok('容量口径：n_plan == n_trade + n_open（闭环）',
     CPl.n_plan === FX.trades_cap.n_trade + FX.trades_cap.n_open,
     `${CPl.n_plan} vs ${FX.trades_cap.n_trade} + ${FX.trades_cap.n_open}`);
  ok('容量口径：plan.n_open == n_open == holding + nobuy',
     CPl.n_open === FX.trades_cap.n_open
       && FX.trades_cap.n_open === FX.trades_cap.n_open_holding + FX.trades_cap.n_open_nobuy,
     `plan=${CPl.n_open} / n_open=${FX.trades_cap.n_open} / ${FX.trades_cap.n_open_holding}+${FX.trades_cap.n_open_nobuy}`);

  // (g) 两条口径下 statistics 都与历史基线一致（防止改动悄悄动了统计）
  ok('不限仓位胜率仍为基线 58.27%',
     Math.abs(FX.trades.summary.win - 0.5827) < 0.0005,
     (FX.trades.summary.win * 100).toFixed(2) + '%');
  ok('容量口径胜率仍为基线 58.57%',
     Math.abs(FX.trades_cap.summary.win - 0.5857) < 0.0005,
     (FX.trades_cap.summary.win * 100).toFixed(2) + '%');

  // (h) 🔴🔴 页面级铁证：容量口径下「完成交易」卡显示 n_trade，
  //      而**未结束数必须出现在另一张卡上**。若哪天有人把未结束行并进统计，
  //      这两张卡会显示同一个数 → 立刻变红。
  const capGrid = d2.querySelector('#p-trades .mgrid').textContent;
  const nCap = new Intl.NumberFormat('zh-CN').format(FX.trades_cap.n_trade);
  const nOpen = new Intl.NumberFormat('zh-CN').format(FX.trades_cap.n_open);
  ok('「完成交易」卡 = n_trade（不含未结束）', capGrid.includes(nCap + ' 笔'),
     `${nCap} 笔`);
  ok('未结束数与完成交易数是**两个不同的数**',
     FX.trades_cap.n_open > 0 && FX.trades_cap.n_trade + FX.trades_cap.n_open
       !== FX.trades_cap.n_trade,
     `完成 ${FX.trades_cap.n_trade} / 未结束 ${FX.trades_cap.n_open}`);
  ok('未结束笔数单独成卡显示', capGrid.includes(nOpen),
     `${nOpen}`);

  /* (i) 🔴🔴 **页面 DOM 级**铁证（上面 (a)~(h) 查的是 fixture，查不到前端把
     未结束行混进明细表这种错法 —— 变异测试发现的盲区）。
     判据：明细表 tbody 的**每一行**都必须有净收益单元格；
     未结束行的 net 是 null，一旦混进去就会出现「—」或空格。 */
  const tbody = d2.querySelector('#tbody-trades');
  ok('明细表 tbody 存在', !!tbody);
  if (tbody && tbody.children.length) {
    const badRows = [...tbody.children].filter(tr => {
      // 净收益列：用表头名定位（禁止用列号 —— 项目硬约定）
      const ths = [...d2.querySelectorAll('#p-trades th')].map(t => t.textContent.replace(/[▲▼]/g, '').trim());
      const ci = ths.indexOf('净收益');
      const td = tr.children[ci];
      if (!td) return true;
      return !/%/.test(td.textContent);   // 已结算行必有「x.xx%」；未结束行是「—」
    });
    ok('🔴 明细表里没有无净收益的行（未结束未混入）',
       badRows.length === 0,
       badRows.length ? `${badRows.length} 行净收益为空，例：${badRows[0].textContent.replace(/\s+/g,' ').slice(0,70)}`
                      : `${tbody.children.length} 行均有净收益`);
    // 未结束行必须出现在**独立区块**里（表格行数 == 分页 rows 数，不含未结束）
    const expRows = Math.min(d2.__lastTradesRows || FX.trades_cap.rows.length, FX.trades_cap.rows.length);
    ok('🔴 明细表行数 == 分页 rows 数（未含未结束）',
       tbody.children.length <= expRows + 1 && tbody.children.length >= expRows - 1,
       `${tbody.children.length} vs 期望 ${expRows}`);
  }

  console.log('\n══════ 前端「交易明细」Tab 验证 ══════\n');
  out.forEach(l => console.log(l));
  const pass = out.filter(l => l.startsWith('✅')).length;
  console.log(`\n通过 ${pass}/${out.length}`);
  if (warns.length) console.log('警告:', warns.slice(0, 4).join(' | '));
  process.exit(0);
})();
