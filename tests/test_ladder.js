/* 前端「⑧ 仓位约束 → 建仓节奏（阶梯建仓）」UI 验证（jsdom 无头）
 *
 * 阶梯语义（与 v3b_lib.plan_positions 的 ladder 参数一致）：
 *   序列里的每个数 = **该批买入的股票只数**；
 *   满仓只数 = sum(ladder)（= 同时持仓上限）；每只等分资金 = 1/sum；
 *   每次建仓把持仓补到下一个累计目标 cumsum(ladder)，买满即停，绝不超配。
 *   ⚠️ 同时持仓 与 每日买入 都由序列推出 → 两个输入框被接管并**禁用**。
 *
 * 覆盖点：
 *   1. 控件齐全（节奏下拉 / 每批只数输入 / 提示位）
 *   2. 初始 = 等权（输入行与提示隐藏）
 *   3. 选预设 → 输入行与柱状示意渲染、满仓只数/每只资金正确
 *   4. 满仓只数 = 序列之和 → 自动接管并**禁用**「同时持仓」「每日买入」
 *   5. 切回等权 → 解除禁用并还原两个原值
 *   6. 手输非预设序列 → 下拉自动切「自定义」
 *   7. 非法输入 → 提示无效、不接管两个输入框
 *   8. 参数收集：cap_ladder 发原始字符串；「不限仓位」口径下必须发 null
 *   9. 诊断卡按阶梯口径渲染（平均仓位 / 真·满仓日）
 *  10. 无运行时错误
 */
const fs = require('fs');
const path = require('path');
const ROOT = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'app/index.html'), 'utf8');
const { JSDOM } = require(path.join(ROOT, 'node_modules/jsdom'));
const F = n => JSON.parse(fs.readFileSync(path.join(__dirname, `fixtures/fixture_${n}.json`), 'utf8'));
const FX = { meta: F('meta'), defaults: F('defaults'), backtest: F('backtest'), scan: F('scan') };

const errors = [];
const warns = [];
const reqs = [];

const dom = new JSDOM(html, {
  runScripts: 'dangerously',
  pretendToBeVisual: true,
  url: 'http://127.0.0.1:8771/',
  beforeParse(w) {
    w.HTMLElement.prototype.scrollIntoView = () => {};
    w.requestAnimationFrame = cb => setTimeout(() => cb(Date.now()), 0);
    w.echarts = {
      init(el) {
        return {
          setOption() {}, resize() {}, dispose() {}, getDom: () => el,
        };
      },
    };
    w.fetch = async (url, opt) => {
      const u = String(url);
      let body = null;
      let payload = null;
      if (opt && opt.body) {
        try { payload = JSON.parse(opt.body); } catch (e) { /* 非 JSON */ }
      }
      reqs.push({ url: u, method: (opt && opt.method) || 'GET', payload });
      if (u.includes('/api/meta')) body = FX.meta;
      else if (u.includes('/api/defaults')) body = FX.defaults;
      else if (u.includes('/api/backtest')) body = FX.backtest;
      else if (u.includes('/api/scan')) body = FX.scan;
      else if (u.includes('/api/trades')) body = { error: '测试不请求明细' };
      else if (u.includes('/api/tune')) body = { results: [], best: null, n: 0, ms: 0 };
      else if (u.includes('/api/stock')) body = { code: '000001.SZ', name: 'T', ind: 'I', rows: [], signal_dates: [] };
      else body = {};
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
    w.console.warn = (...a) => { warns.push(a.join(' ')); };
  },
});

const w = dom.window, d = w.document;
const $ = s => d.querySelector(s), $$ = s => [...d.querySelectorAll(s)];

setTimeout(() => {
  const out = [];
  const ok = (t, c, x) => out.push(`${c ? '✅' : '❌'} ${t}${x !== undefined ? '  → ' + x : ''}`);
  const ev = (el, t) => el.dispatchEvent(new w.Event(t, { bubbles: true }));
  const click = el => el.dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  const setSeg = (sel, v) => $$(sel + ' button').forEach(b =>
    b.classList.toggle('on', b.dataset.v === v));

  const mpEl = $('#cap_maxpos'), mnEl = $('#cap_maxnew');

  // ---- 0. fixture 前提
  const PRESETS = FX.defaults.ladder_presets || [];
  ok('fixture_defaults 含 ladder_presets', PRESETS.length > 0,
     `${PRESETS.length} 条`);

  // ---- 1. 控件齐全
  const need = ['cap_rhythm', 'cap_ladder', 'cap_ladder_row', 'cap_ladder_info'];
  const miss = need.filter(i => !$('#' + i));
  ok('建仓节奏控件齐全（4 个）', miss.length === 0, miss.length ? '缺: ' + miss : '全部存在');
  ok('第 ⑥ 个控件是「每批只数」输入（单位 只/批）',
     /每批只数/.test($('#cap_ladder_row').textContent)
     && /只\/批/.test($('#cap_ladder_row').textContent),
     $('#cap_ladder_row').textContent.replace(/\s+/g, ' ').trim());

  // ---- 2. 初始状态 = 等权
  ok('初始下拉 = 等权（空值）', $('#cap_rhythm').value === '',
     `value="${$('#cap_rhythm').value}"`);
  ok('初始只数输入行隐藏', $('#cap_ladder_row').style.display === 'none');
  ok('初始提示位隐藏', $('#cap_ladder_info').style.display === 'none');
  const optVals = [...$('#cap_rhythm').options].map(o => o.value);
  ok('下拉含「等权」+ 全部预设 + 「自定义」',
     optVals[0] === '' && optVals.includes('__custom__')
     && PRESETS.every(p => optVals.includes(p.ladder.join(','))),
     optVals.join(' | '));

  // ---- 3. 选预设 → 渲染
  setSeg('#seg-cap', '1');
  // 先把两个输入框改成非默认值，用来验证「接管 + 还原」不是巧合
  mpEl.value = '8'; mnEl.value = '5';
  const lad = [1, 2, 2, 2, 3];
  const SUM = lad.reduce((a, b) => a + b, 0);        // 满仓只数 = 10
  $('#cap_rhythm').value = lad.join(',');
  ev($('#cap_rhythm'), 'change');
  ok('选预设 → 只数输入行显示', $('#cap_ladder_row').style.display !== 'none');
  ok('选预设 → 提示位显示', $('#cap_ladder_info').style.display !== 'none');
  ok('选预设 → 输入框同步为预设值', $('#cap_ladder').value === '1,2,2,2,3',
     $('#cap_ladder').value);
  const infoTxt = $('#cap_ladder_info').textContent.replace(/\s+/g, ' ');
  ok('提示含「10 只满仓」（= 序列之和）', /10\s*只满仓/.test(infoTxt), infoTxt.slice(0, 90));
  ok('提示含「5 批建仓」', /5\s*批建仓/.test(infoTxt), infoTxt.slice(0, 90));
  ok('提示含每只资金 10%', /每只\s*10%/.test(infoTxt), infoTxt.slice(0, 110));
  ok('提示含累计目标 1 → 3 → 5 → 7 → 10',
     /1\s*→\s*3\s*→\s*5\s*→\s*7\s*→\s*10/.test(infoTxt), infoTxt.slice(0, 200));
  const bars = $$('#cap_ladder_info .ladbar');
  ok('柱状示意 5 根', bars.length === 5, `实际 ${bars.length}`);
  const barTxt = bars.map(b => b.textContent.trim()).join(',');
  ok('柱状示意数值 = 1,2,2,2,3（每批只数）', barTxt === '1,2,2,2,3', barTxt);
  const heights = bars.map(b => parseFloat(/height:\s*([\d.]+)%/.exec(b.querySelector('i').getAttribute('style'))[1]));
  ok('柱高与只数成正比（1 最低、3 最高）',
     heights[0] < heights[1] && heights[1] === heights[2]
     && Math.abs(heights[4] - 100) < 0.01 && Math.abs(heights[0] - 33.33) < 0.5,
     heights.join(','));

  // ---- 4. 接管「同时持仓」与「每日买入」
  ok('满仓只数 = 序列之和 → 同时持仓被改写为 10', mpEl.value === String(SUM), mpEl.value);
  ok('同时持仓被禁用', mpEl.disabled === true);
  ok('同时持仓 title 说明「序列之和」', /序列之和/.test(mpEl.title || ''), mpEl.title);
  ok('每日买入被禁用（由序列推出）', mnEl.disabled === true);
  ok('每日买入 = 最大单批只数 3', mnEl.value === '3', mnEl.value);
  ok('每日买入 title 说明「最大单批」', /最大单批/.test(mnEl.title || ''), mnEl.title);

  // ---- 5. 切回等权 → 还原两个输入框
  $('#cap_rhythm').value = '';
  ev($('#cap_rhythm'), 'change');
  ok('切回等权 → 输入行隐藏', $('#cap_ladder_row').style.display === 'none');
  ok('切回等权 → 同时持仓解除禁用', mpEl.disabled === false);
  ok('切回等权 → 同时持仓还原为原值 8', mpEl.value === '8', mpEl.value);
  ok('切回等权 → 每日买入解除禁用', mnEl.disabled === false);
  ok('切回等权 → 每日买入还原为原值 5', mnEl.value === '5', mnEl.value);

  // ---- 5b. 点「快捷预设」→ 应自动关掉阶梯，并把两个输入框还给用户
  //      （快捷预设本身就是等权配置；若不关阶梯，两个框还处于禁用接管态）
  $('#cap_rhythm').value = lad.join(','); ev($('#cap_rhythm'), 'change');
  click($$('.capquick button')[0]);            // 10只 / 日3只
  ok('点快捷预设 → 自动切回等权', $('#cap_rhythm').value === '', $('#cap_rhythm').value);
  ok('点快捷预设 → 同时持仓 = 10 且可编辑',
     mpEl.value === '10' && mpEl.disabled === false, `${mpEl.value}/${mpEl.disabled}`);
  ok('点快捷预设 → 每日买入 = 3 且可编辑',
     mnEl.value === '3' && mnEl.disabled === false, `${mnEl.value}/${mnEl.disabled}`);
  setSeg('#seg-cap', '0');

  // ---- 6. 手输非预设 → 下拉自动切「自定义」
  $('#cap_rhythm').value = lad.join(','); ev($('#cap_rhythm'), 'change');
  $('#cap_ladder').value = '1,1,2,3,3'; ev($('#cap_ladder'), 'input');
  ok('手输匹配预设 → 下拉自动对上', $('#cap_rhythm').value === '1,1,2,3,3',
     $('#cap_rhythm').value);
  $('#cap_ladder').value = '1,3,6'; ev($('#cap_ladder'), 'input');
  ok('手输非预设 → 下拉切「自定义」', $('#cap_rhythm').value === '__custom__',
     $('#cap_rhythm').value);
  const info3 = $('#cap_ladder_info').textContent.replace(/\s+/g, ' ');
  ok('自定义 1,3,6 → 满仓 10 只（序列之和）', /10\s*只满仓/.test(info3), info3.slice(0, 90));
  ok('自定义 1,3,6 → 累计目标 1 → 4 → 10',
     /1\s*→\s*4\s*→\s*10/.test(info3), info3.slice(0, 200));
  ok('自定义 → 同时持仓被改写为 10', mpEl.value === '10', mpEl.value);
  ok('自定义 → 每日买入 = 最大单批 6', mnEl.value === '6', mnEl.value);

  // ---- 7. 非法输入
  // ⚠️ 判据是「每个元素四舍五入后是否 ≥1」：'1,,2' 只是多打了个逗号，
  //    前后端都会宽容地解析成 [1,2]（见 engine.parse_ladder），不算非法。
  for (const bad of ['abc', '1,-2', '0,1', '1,x', '--', '0.4,2']) {
    $('#cap_ladder').value = bad; ev($('#cap_ladder'), 'input');
    const t = $('#cap_ladder_info').textContent;
    if (!/无效/.test(t) || !/等权/.test(t)) {
      ok(`非法输入「${bad}」提示无效`, false, t.slice(0, 60));
    }
    if (mpEl.disabled || mnEl.disabled) {
      ok(`非法输入「${bad}」不应接管同时持仓/每日买入`, false,
         `mp=${mpEl.disabled} mn=${mnEl.disabled}`);
    }
  }
  ok('非法输入：提示无效 + 不接管两个输入框', true, '6 种非法值全部符合');

  // 宽容：多余分隔符 / 全角逗号 / 斜杠 / 空格 / 小数四舍五入
  for (const [raw, want] of [['1,,2', '1,2'], ['1，2，2', '1,2,2'],
                             ['1/2/3', '1,2,3'], ['1 2 2', '1,2,2'],
                             ['1.6,2.4', '2,2']]) {
    $('#cap_ladder').value = raw; ev($('#cap_ladder'), 'input');
    const n = $$('#cap_ladder_info .ladbar').length;
    const w2 = want.split(',');
    if (n !== w2.length) {
      ok(`宽容解析「${raw}」→ ${want}`, false, `${n} 根柱`);
    } else {
      const got = $$('#cap_ladder_info .ladbar').map(b => b.textContent.trim()).join(',');
      if (got !== want) ok(`宽容解析「${raw}」→ ${want}`, false, `得到 ${got}`);
    }
  }
  ok('宽容解析：分隔符 / 全角逗号 / 斜杠 / 空格 / 小数取整', true, '5 种写法全部识别');

  // 空串：应保持「自定义」并提示无效，不偷偷关掉阶梯
  $('#cap_ladder').value = ''; ev($('#cap_ladder'), 'input');
  ok('清空输入框 → 保持「自定义」（不偷偷切等权）',
     $('#cap_rhythm').value === '__custom__', $('#cap_rhythm').value);
  ok('清空输入框 → 提示按等权处理',
     /无效/.test($('#cap_ladder_info').textContent));

  // ---- 8. 参数收集
  const collect = () => {
    const before = reqs.length;
    click($('#run'));
    const bt = reqs.slice(before).filter(r => r.url.includes('/api/backtest')).pop();
    return bt && bt.payload ? (bt.payload.params || bt.payload) : null;
  };
  setSeg('#seg-cap', '1');
  $('#cap_rhythm').value = lad.join(','); ev($('#cap_rhythm'), 'change');
  let p = collect();
  ok('阶梯启用 → 请求体带 cap_ladder 原始字符串',
     p && p.cap_ladder === '1,2,2,2,3', p ? JSON.stringify(p.cap_ladder) : '—');
  ok('阶梯启用 → max_pos 已同步为 10（序列之和）', p && p.max_pos === 10,
     p ? String(p.max_pos) : '—');

  $('#cap_rhythm').value = '__custom__'; ev($('#cap_rhythm'), 'change');
  $('#cap_ladder').value = '2,3,5'; ev($('#cap_ladder'), 'input');
  p = collect();
  ok('自定义序列原样上报（后端做权威解析）',
     p && p.cap_ladder === '2,3,5', p ? JSON.stringify(p.cap_ladder) : '—');

  // 关键契约：切回「不限仓位」必须发 null，否则残留序列会让后端仍按容量约束算
  setSeg('#seg-cap', '0');
  p = collect();
  ok('「不限仓位」口径 → cap_ladder 必须为 null', p && p.cap_ladder === null,
     p ? JSON.stringify(p.cap_ladder) : '—');
  ok('「不限仓位」口径 → max_pos = 0', p && p.max_pos === 0, p ? String(p.max_pos) : '—');
  setSeg('#seg-cap', '1');

  // ---- 9. 诊断卡按阶梯渲染
  // ⚠️ 这里用**构造的 capacity**：fixture_backtest 是「不限仓位」口径刷出来的，
  //    本来就不含 capacity 块。字段名与 engine.capacity() 输出逐一对齐
  //    （max_pos = 满仓只数 = sum(ladder)）；真实数值由
  //    scripts/verify_ladder.py / verify_ladder_http.py 跑真面板验证。
  const mkCap = () => ({
    max_pos: 10, max_new: 3, pick: 'deep', pick_name: '最超跌优先',
    pick_desc: '距MA60 越低越优先（推荐）',
    ladder: [1, 2, 2, 2, 3], ladder_total: 10,
    cagr: 0.3914, mdd: -0.4217, sharpe: 1.2, vol: 0.26, ann_arith: 0.45,
    cap_cagr: 0.3261, cap_mdd: -0.4217, cap_sharpe: 1.1, cap_vol: 0.25,
    n_hold: 410, n_signal: 105851, n_drop: 105441, drop_pct: 0.9961,
    n_drop_dup: 0, fill_pct: 0.0039, avg_pos: 9.2, max_pos_seen: 10,
    empty_pct: 0.1, active_pct: 0.9,
    avg_invested: 0.914, full_pct: 0.512, max_invested: 1.0,
    yearly: [], segs: {},
  });
  FX.backtest = Object.assign({}, FX.backtest, { capacity: mkCap() });
  $('#cap_rhythm').value = lad.join(','); ev($('#cap_rhythm'), 'change');
  click($('#run'));
  setTimeout(() => {
    const el = $('#p-bt');
    const body = el ? el.textContent.replace(/\s+/g, ' ') : '';
    ok('诊断卡出现「阶梯建仓」说明', /阶梯建仓/.test(body),
       (body.match(/建仓节奏：阶梯建仓[^。]{0,50}/) || ['（未找到）'])[0]);
    ok('诊断卡显示满仓只数 10（= 序列之和）', /满仓\s*10\s*只/.test(body),
       (body.match(/满仓\s*\d+\s*只/g) || []).slice(0, 3).join(','));
    ok('诊断卡显示「平均仓位」而非「资金利用率」', /平均仓位/.test(body));
    ok('诊断卡显示「真·满仓日占比」', /真\s*·?\s*满仓日/.test(body));
    ok('诊断卡账户口径副标题提到「每只等分资金」', /每只等分资金/.test(body),
       (body.match(/每只等分资金[^。]{0,30}/) || ['（未找到）'])[0]);
    ok('诊断卡显示平均仓位数值 91.4%', /91\.4%/.test(body),
       (body.match(/\d+\.\d%/g) || []).slice(0, 8).join(','));
    finish();
  }, 1400);

  function finish() {
    ok('无运行时错误', errors.length === 0, errors.slice(0, 3).join(' | ') || '干净');
    console.log('\n══════ 前端「建仓节奏（阶梯建仓）」UI 验证 ══════\n');
    out.forEach(l => console.log(l));
    const pass = out.filter(l => l.startsWith('✅')).length;
    console.log(`\n通过 ${pass}/${out.length}`);
    if (warns.length) console.log('警告:', warns.slice(0, 4).join(' | '));
    process.exit(0);
  }
}, 2600);
