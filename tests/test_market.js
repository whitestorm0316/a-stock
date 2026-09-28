/* 前端「市场环境」块验证（jsdom 无头）。
 *
 * 背景（真实用户提问触发的）：
 *   用户说「我很确认今天是熊市 <60，为什么你界面说不是？」—— 而界面当时只给一句
 *   结论「最新交易日市场处于 MA60 上方（不满足）」，没有任何依据可查。
 *
 *   根因是**两条线不是同一条线**：
 *     · 策略的牛熊判定用的是**自建全A等权净值**（`market_oret` = 当日全市场
 *       「可交易开盘→开盘」收益的等权平均，逐日复利）。它波动更大、长期上偏，
 *       与交易所指数（上证/沪深300）在个别时段会明显分歧。
 *     · 指数数据本地只覆盖 2023 起（`scripts/01c_fetch_index.py` 的取数窗口限制），
 *       做不了 11 年回测，所以只能自建。
 *
 *   修法不是「把结论改成用户想要的」，而是**把依据摆出来**：并列展示自建净值与
 *   真实指数的同口径（最新值 vs 自身 MA60），分歧时显式告警。
 *
 * 覆盖点：
 *   1. fixture_market 结构（自建序列 + 4 个真实指数）
 *   2. 引擎口径行带「策略用」标记（用户要知道**策略实际用哪条**）
 *   3. 4 个真实指数都渲染出「距MA60 / 状态 / 数据日」
 *   4. 引擎说牛市、而真实指数里有熊市 → 必须出现「口径分歧」告警
 *   5. 两者一致时**不能**误报告警（否则告警会被无视）
 *   6. `bullBadge(null)` → 「—」而不是「熊市」（均线未成形 ≠ 跌破均线）
 *   7. 选股页确实发起了 GET /api/market
 *   8. 无运行时错误
 *   9. **市场指数口径 / 均线窗口两个下拉**（后加的 UI 选项）：
 *      · 选项来自 /api/meta，默认 all / 60；
 *      · ⚠️ 切换后**必须重新请求** —— 换口径会改变 mask，不能吃旧缓存；
 *      · 表格、条件徽标、警告文案都要跟着变；
 *      · 表格里要并列「同一口径下的三个窗口」（换窗口的敏感性依据）。
 */
const fs = require('fs');
const path = require('path');
const ROOT = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'app/index.html'), 'utf8');
const { JSDOM } = require(path.join(ROOT, 'node_modules/jsdom'));
const F = n => JSON.parse(fs.readFileSync(path.join(__dirname, `fixtures/fixture_${n}.json`), 'utf8'));
const FX = {
  meta: F('meta'), defaults: F('defaults'), scan: F('scan'),
  market: F('market'), backtest: F('backtest'),
};

const errors = [];
const reqs = [];

const dom = new JSDOM(html, {
  runScripts: 'dangerously',
  pretendToBeVisual: true,
  url: 'http://127.0.0.1:8771/',
  beforeParse(w) {
    w.HTMLElement.prototype.scrollIntoView = () => {};
    w.requestAnimationFrame = cb => setTimeout(() => cb(Date.now()), 0);
    w.echarts = {
      init(el) { return { setOption() {}, resize() {}, dispose() {}, getDom: () => el }; },
    };
    w.fetch = async (url, opt) => {
      const u = String(url);
      let payload = null;
      if (opt && opt.body) { try { payload = JSON.parse(opt.body); } catch (e) { /* 非 JSON */ } }
      reqs.push({ url: u, method: (opt && opt.method) || 'GET', payload });
      let body = {};
      if (u.includes('/api/meta')) body = FX.meta;
      else if (u.includes('/api/defaults')) body = FX.defaults;
      else if (u.includes('/api/market')) {
        // ⚠️ mock 必须跟随 index/ma 变化 —— 否则「切换口径后表格是否跟随」根本测不出来
        //    （mock 恒返回默认口径的 fixture，表格永远显示全A等权，断言形同虚设）。
        const ix = (u.match(/[?&]index=([^&]*)/) || [])[1] || 'all';
        const ma = (u.match(/[?&]ma=([^&]*)/) || [])[1] || '60';
        body = JSON.parse(JSON.stringify(FX.market));
        body.index_key = ix;
        body.ma_win = parseInt(ma, 10);
        if (ix !== 'all') {
          body.index_name = (ix === 'small50') ? '小50% 等权' : ix;
          body.dist_now = 5.9;
          body.bull_now = true;
          body.series = body.series.map(s => Object.assign({}, s, { nav: s.nav * 1.5 }));
        }
      }
      else if (u.includes('/api/scan')) body = FX.scan;
      else if (u.includes('/api/backtest')) body = FX.backtest;
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

/* 市场环境表：取「自建净值」那一行与真实指数行的单元格。
   ⚠️ 按**行首文本**定位，不按行号 —— 表里加一行（如再多一个指数）不该让断言失效。 */
function mktRows() {
  const box = $('#p-scan');
  if (!box) return [];
  return $$('#p-scan table').flatMap(tb => [...tb.querySelectorAll('tbody tr')])
    .map(tr => [...tr.children].map(td => td.textContent.replace(/\s+/g, ' ').trim()));
}
const rowBy = kw => mktRows().find(r => (r[0] || '').includes(kw));
const scanTxt = () => ($('#p-scan').textContent || '').replace(/\s+/g, ' ');

setTimeout(async () => {
  // ---- 0. fixture 前提
  const mk = FX.market;
  ok('fixture_market 有自建净值序列', Array.isArray(mk.series) && mk.series.length > 60,
     `${(mk.series || []).length} 条`);
  ok('fixture_market 有 4 个真实指数', (mk.indices || []).length === 4,
     (mk.indices || []).map(x => x.name).join(','));
  ok('fixture_market 的 last_date 与 /api/meta 一致',
     mk.last_date === FX.meta.last_date, `${mk.last_date} vs ${FX.meta.last_date}`);
  ok('自建净值序列最后一项 nav 与「最新值」同源',
     mk.series[mk.series.length - 1].nav !== undefined);
  // 这是本次问题的**事实前提**：引擎说牛市，而真实指数里有熊市。
  // 若哪天数据变了、两边一致了，这条会失败 —— 那是提醒去复核，不是 bug。
  const bears = (mk.indices || []).filter(x => x.bull === false);
  ok('fixture 里存在「引擎牛市 + 真实指数有熊市」的分歧',
     mk.bull_now === true && bears.length > 0,
     `引擎 bull_now=${mk.bull_now}，熊市指数 ${bears.map(x => x.name).join(',') || '无'}`);

  // ---- 1. 市场环境序列的请求行为
  //   ⚠️ 不能用「点一次 #run 就应看到一次 /api/market」来断言：页面加载时会自动
  //      跑一次，`loadMarket()` 已把结果缓存在会话里，之后再点**不该**重复请求。
  //      所以要分两条断：先「确实请求过」，再「不重复请求」。
  const mktReq = () => reqs.filter(q => q.url.includes('/api/market'));
  ok('页面加载后确实请求过 /api/market', mktReq().length >= 1,
     reqs.map(q => `${q.method} ${q.url.replace(/^https?:\/\/[^/]+/, '')}`).join(' | ') || '（无请求）');
  ok('该请求是 GET（不是 POST）', mktReq().every(q => q.method === 'GET'),
     mktReq().map(q => q.method).join(',') || '（无）');
  const nBefore = mktReq().length;
  click($('#run'));
  await new Promise(r => setTimeout(r, 1500));
  ok('同一会话内不重复请求 /api/market（结果已缓存）',
     mktReq().length === nBefore, `之前 ${nBefore} 次 → 现在 ${mktReq().length} 次`);
  ok('选股确实重新请求了 /api/scan', reqs.filter(q => q.url.includes('/api/scan')).length >= 2,
     reqs.filter(q => q.url.includes('/api/scan')).length + ' 次');

  // ---- 1b. 市场指数口径 / 均线窗口两个下拉
  const selI = $('#mkt_index'), selM = $('#mkt_ma');
  ok('左侧有「指数口径」下拉', !!selI, selI ? `${selI.options.length} 个选项` : '（缺失）');
  ok('左侧有「均线窗口」下拉', !!selM, selM ? `${selM.options.length} 个选项` : '（缺失）');
  ok('口径选项数与 /api/meta 一致（前端不自己维护一份）',
     !!selI && selI.options.length === (FX.meta.mkt_indexes || []).length,
     selI ? `${selI.options.length} vs meta ${(FX.meta.mkt_indexes || []).length}` : '（缺失）');
  ok('默认选中 全A等权 / MA60',
     !!selI && selI.value === 'all' && !!selM && selM.value === '60',
     `${selI && selI.value} / ${selM && selM.value}`);
  ok('默认口径下不显示「非默认口径」警告',
     !/非默认口径/.test($('#mkt_cfg_note').textContent));
  ok('市场环境表含「同一口径 · 换均线窗口」小节（换窗口敏感性的依据）',
     /换均线窗口/.test(scanTxt()),
     (scanTxt().match(/换均线窗口[^表]{0,20}/) || ['（未找到）'])[0]);
  ok('该小节把当前窗口标出来', /MA60 当前/.test(scanTxt()),
     (scanTxt().match(/MA\d+ 当前/) || ['（未找到）'])[0]);
  ok('口径行标签随窗口变化（不再是写死的 MA60）',
     !!rowBy('自建全A等权净值') && scanTxt().includes('距MA60'),
     '默认窗口下应为 距MA60');

  // ---- 1c. ⚠️ 切换口径**必须重新请求**（换口径会改变 mask，不能吃旧缓存）
  const nBeforeSwitch = mktReq().length;
  selI.value = 'small50';
  selI.dispatchEvent(new w.Event('change'));
  await new Promise(r => setTimeout(r, 1500));
  const sw = mktReq().slice(nBeforeSwitch);
  ok('切换口径后重新请求 /api/market（不吃旧缓存）', sw.length >= 1,
     `${nBeforeSwitch} 次 → ${mktReq().length} 次`);
  ok('新请求带上了 index=small50', sw.some(q => /index=small50/.test(q.url)),
     sw.map(q => q.url.replace(/^https?:\/\/[^/]+/, '')).join(' | ') || '（无）');
  ok('切换口径后表格跟随（显示自建小50% 等权净值）',
     /自建小50% 等权净值/.test(scanTxt()),
     (scanTxt().match(/自建[^净]{0,14}净值/) || ['（未找到）'])[0]);
  ok('切换后出现「非默认口径」警告（含换窗口等价性提示）',
     /非默认口径/.test($('#mkt_cfg_note').textContent)
     && /换均线窗口的效果几乎等价/.test($('#mkt_cfg_note').textContent));
  ok('顶部条件徽标跟随口径', /小50% 等权/.test($('#bmode').textContent),
     $('#bmode').textContent);

  // 切回默认，避免污染后续断言（也顺带验证「切回去也要重取」）
  const nBack = mktReq().length;
  selI.value = 'all';
  selI.dispatchEvent(new w.Event('change'));
  await new Promise(r => setTimeout(r, 1500));
  ok('切回默认口径同样重新请求', mktReq().length > nBack,
     `${nBack} 次 → ${mktReq().length} 次`);
  ok('切回默认后警告消失', !/非默认口径/.test($('#mkt_cfg_note').textContent));

  // ---- 2. 自建净值那一行（策略实际用的那条）
  const eng = rowBy('自建全A等权净值');
  ok('市场环境表含「自建全A等权净值」行', !!eng, eng ? eng.join(' | ') : '（未找到）');
  if (eng) {
    ok('该行标注「策略用」（用户要知道策略实际用哪条）', eng[0].includes('策略用'), eng[0]);
    ok('该行距MA60 = 后端 dist_now',
       eng[3] === `${Number(mk.dist_now).toFixed(2)}%`, `${eng[3]} vs ${mk.dist_now}%`);
    ok('该行状态与后端 bull_now 一致',
       eng[4] === (mk.bull_now ? '牛市' : '熊市'), eng[4]);
    ok('该行数据日 = 最新交易日', eng[5] === mk.last_date, eng[5]);
  }

  // ---- 3. 4 个真实指数都渲染出来
  const names = (mk.indices || []).map(x => x.name);
  const missing = names.filter(n => !rowBy(n));
  ok(`真实指数 ${names.length} 个全部渲染`, missing.length === 0,
     missing.length ? `缺 ${missing.join(',')}` : names.join(','));
  const sh = rowBy('上证指数');
  if (sh) {
    ok('上证指数行距MA60 为负（当前是熊市）', sh[3].startsWith('-'), sh[3]);
    ok('上证指数行状态 = 熊市', sh[4] === '熊市', sh[4]);
  }

  // ---- 4. 分歧告警必须出现
  ok('出现「口径分歧」告警', /口径分歧/.test(scanTxt()),
     (scanTxt().match(/口径分歧[^。]{0,60}/) || ['（未找到）'])[0]);
  ok('告警说明了「策略实际用自建净值」', /策略实际用的是自建净值/.test(scanTxt()));

  // ---- 5. 一致时不得误报（直接调 mktBlock，绕开 fixture 固定值）
  const mkSame = {
    last_date: mk.last_date, bull_now: false, dist_now: -1.0,
    series: mk.series.slice(-3),
    indices: mk.indices.map(x => Object.assign({}, x, { bull: false })),
  };
  const htmlSame = w.mktBlock(mkSame);
  ok('两边都是熊市时**不**出现分歧告警', !/口径分歧/.test(htmlSame));
  const mkNoIdx = { last_date: mk.last_date, bull_now: true, dist_now: 1, series: mk.series.slice(-3), indices: [] };
  ok('没有指数数据时也不告警（可选数据缺失不该刷警告）',
     !/口径分歧/.test(w.mktBlock(mkNoIdx)));
  ok('没有指数数据时仍渲染自建净值行', /自建全A等权净值/.test(w.mktBlock(mkNoIdx)));

  // ---- 6. 均线未成形 ≠ 跌破均线
  //    `bullBadge` 是顶层 const，不在 window 上，用 eval 取（同一 realm 的全局词法作用域）
  const badgeNull = String(w.eval('bullBadge(null)'));
  ok('bullBadge(null) 显示「—」而不是「熊市」', /—/.test(badgeNull) && !/熊市/.test(badgeNull),
     badgeNull);
  ok('bullBadge(false) 才是「熊市」', /熊市/.test(String(w.eval('bullBadge(false)'))));

  // ---- 7. 无运行时错误
  ok('无运行时错误', errors.length === 0, errors.slice(0, 3).join(' | ') || '干净');

  console.log('\n══════ 前端「市场环境（牛熊判定依据）」验证 ══════\n');
  out.forEach(l => console.log(l));
  const pass = out.filter(l => l.startsWith('✅')).length;
  console.log(`\n通过 ${pass}/${out.length}`);
  process.exit(0);
}, 900);
