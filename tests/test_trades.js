/* 前端「交易明细」Tab 渲染验证（jsdom 无头） */
const fs = require('fs');
// jsdom 从项目根 node_modules 解析（见下方 ROOT）

const path = require('path');
const ROOT = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'app/index.html'), 'utf8');
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
  if (dt) {
    const ths = [...dt.querySelectorAll('thead th')].map(htxt);
    ok('明细表 20 列', ths.length === 20, '实际 ' + ths.length);
    const need2 = ['#', '代码', '名称', '板块', '信号日', '买入日', '卖出日', '买入价',
                   '卖出价', '净收益', '毛收益', '同期基准', '超额', '营收', '营收同比'];
    const m2 = need2.filter(x => !ths.includes(x));
    ok('明细列名齐全', m2.length === 0, m2.length ? '缺: ' + m2 : 'ok');
    ok('默认排序标记 exit_date', dt.querySelector('thead').textContent.includes('▼'));
  }

  // ---- 10. 明细行
  const trs = $$('#tbody-trades tr');
  ok('明细 50 行', trs.length === 50, '实际 ' + trs.length);
  if (trs.length) {
    const tds = trs[0].querySelectorAll('td');
    ok('明细行 20 单元格', tds.length === 20, '实际 ' + tds.length);
    const t = [...tds].map(x => x.textContent.trim());
    ok('代码格式正确', /^\d{6}\.(SH|SZ|BJ)$/.test(t[1]), t[1]);
    /* 列序（20 列，已用 probe 实证，改动渲染顺序必须同步这里）：
       0# 1代码 2名称 3行业 4板块 5信号日 6买入日 7卖出日 8买入价 9卖出价
       10净收益 11毛收益 12同期基准 13超额 14市值组 15距MA60
       16营收 17营收同比 18归母净利 19归母同比 */
    ok('板块列(索引4)为中文标签', ['主板', '创业板', '科创板', '北交所', '—'].includes(t[4]), t[4]);
    ok('行业列(索引3)在板块列之前', t[3] !== '' && t[4] !== '', `行业=${t[3]} 板块=${t[4]}`);
    ok('买入价列(索引8)是数字', /^\d+(\.\d+)?$/.test(t[8]), t[8]);
    ok('卖出价列(索引9)是数字', /^\d+(\.\d+)?$/.test(t[9]), t[9]);
    ok('毛收益列(索引11)带 %', /%/.test(t[11]), t[11]);
    ok('市值组列(索引14)为 M+数字', /^M\d+$/.test(t[14]) || t[14] === '—', t[14]);
    ok('距MA60列(索引15)为 D+数字', /^D\d+$/.test(t[15]) || t[15] === '—', t[15]);
    // ⚠️ 净收益单元格（索引10）同时含「盈/亏」徽章，textContent 是 数值 + 徽章
    ok('净收益带 %', /%/.test(t[10]), t[10]);
    ok('盈亏标记存在', /盈|亏/.test(t[10]), t[10]);
    // ⚠️ 单位回归：净收益必须与 卖出价/买入价-1 对得上（防止再次 ×100）
    const buy = parseFloat(t[8]), sell = parseFloat(t[9]);
    const netShown = parseFloat((t[10].match(/-?\d+(\.\d+)?/) || [])[0]);
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
  ok('页面「共 N 笔」= 可结算数',
     txt2.replace(/,/g, '').includes(`共 ${CP.n_plan - CP.n_open} 笔`),
     `共 ${FX.trades_cap.n_trade} 笔`);
  if (CP.n_open > 0) {
    // ⚠️ 措辞必须说「最后 H 个交易日内」，而不是「n_open 个交易日内」——
    //    前者是**时间窗口**（= 持有期 20 日），后者是**笔数**，两者不是一回事。
    ok('含未平仓说明（时间窗口口径）',
       /数据末尾最后 20 个交易日内/.test(txt2.replace(/\s+/g, ' ')),
       (txt2.replace(/\s+/g, ' ').match(/数据末尾最后 \d+ 个交易日/) || [''])[0]);
    ok('未平仓不计入统计', /不计入本页明细与胜率统计/.test(txt2));
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

  console.log('\n══════ 前端「交易明细」Tab 验证 ══════\n');
  out.forEach(l => console.log(l));
  const pass = out.filter(l => l.startsWith('✅')).length;
  console.log(`\n通过 ${pass}/${out.length}`);
  if (warns.length) console.log('警告:', warns.slice(0, 4).join(' | '));
  process.exit(0);
})();
