/* 前端「参数寻优」页验证（jsdom 无头）—— 重点是**建仓节奏轴**与两套口径。
 *
 * 背景：节奏（同时持几只 / 每日买几只 / 阶梯）与「超额选股规则」都是**容量层**参数，
 *   在「不限仓位」口径下**完全无效**（信号全买时压根没有仓位上限，
 *   先买哪只、分几批买都不影响净值）。所以：
 *     · 只勾信号轴 → stats()，按不限仓位 Sharpe 排序（研究报告口径）
 *     · 勾了 rhythm / pick（或左侧 ⑧ 已开）→ capacity()，按**账户资金口径**排序
 *
 * 覆盖点：
 *   1. 节奏轴 / 选股规则轴两个 checkbox 存在
 *   2. 节奏档数说明由 /api/defaults 的 rhythm_cands 填充（不写死在前端）
 *   3. 未勾容量轴 → 口径提示隐藏；勾上 → 出现
 *   4. 信号口径：表头与排序依据都是「不限仓位」，且**没有**账户口径列
 *   5. 容量口径：表头换成账户口径列，且**不再出现**胜率/PF（避免两套口径混排）
 *   6. 请求体确实带上了 axes.rhythm
 *   7. 「多行同结果」提示：容量口径下的常见现象，必须被解释而不是让用户猜
 *   8. 表头点击可换排序（点「账户CAGR」→ 首行变成最大值；再点 → 最小值）
 *   9. 无运行时错误
 */
const fs = require('fs');
const path = require('path');
const ROOT = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'app/index.html'), 'utf8');
const { JSDOM } = require(path.join(ROOT, 'node_modules/jsdom'));
const F = n => JSON.parse(fs.readFileSync(path.join(__dirname, `fixtures/fixture_${n}.json`), 'utf8'));
const FX = {
  meta: F('meta'), defaults: F('defaults'), scan: F('scan'),
  tune: F('tune'), tune_rhythm: F('tune_rhythm'),
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
      else if (u.includes('/api/scan')) body = FX.scan;
      // 🔴 后端按「有没有勾容量轴」分流两套口径 → 桩也照这个分流
      else if (u.includes('/api/tune')) {
        const ax = (payload && payload.axes) || {};
        body = (ax.rhythm || ax.pick) ? FX.tune_rhythm : FX.tune;
      } else if (u.includes('/api/trades') || u.includes('/api/backtest')) {
        body = { error: '本套件不请求该接口' };
      }
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
const ev = (el, t) => el.dispatchEvent(new w.Event(t, { bubbles: true }));
const sleep = ms => new Promise(r => setTimeout(r, ms));

/* 勾选/取消「#axes」里指定的维度（其余全部取消），并触发 change */
function pickAxes(names) {
  $$('#axes input').forEach(i => { i.checked = names.includes(i.value); });
  ev($('#axes'), 'change');
}
const heads = () => $$('#tune-tblwrap th').map(t => t.textContent.replace(/[▲▼]/g, '').trim());
const paneTxt = () => ($('#tune-out').textContent || '').replace(/\s+/g, ' ');
const bodyRows = () => $$('#tune-tblwrap tbody tr');
const colIdx = name => heads().indexOf(name);
const cell = (tr, name) => {
  const i = colIdx(name);
  return i < 0 ? null : tr.children[i].textContent.trim();
};

setTimeout(async () => {
  // ---- 0. fixture 前提
  ok('fixture_tune 是信号口径（cap_mode=false）', FX.tune.cap_mode === false,
     `cap_mode=${FX.tune.cap_mode}`);
  ok('fixture_tune_rhythm 是容量口径（cap_mode=true）', FX.tune_rhythm.cap_mode === true,
     `cap_mode=${FX.tune_rhythm.cap_mode}`);
  ok('容量 fixture 里确实存在「多行同结果」',
     FX.tune_rhythm.n_distinct_cap < FX.tune_rhythm.n_combo,
     `distinct=${FX.tune_rhythm.n_distinct_cap} / combo=${FX.tune_rhythm.n_combo}`
     + ` / 最多同值=${FX.tune_rhythm.max_same_cap}`);
  // px_ma60 轴的 6 个候选里，5 个是**嵌套**的（D1 / D1~2 / D1~3 / D1~5 / D1~10），
  // 只有 D2~3 不嵌套 → 每个节奏档下这 5 个必然给出同一个账户净值。
  // 若缓存写错（例如指纹漏了信号参数），60 行会全部撞成 1 组，这条断言就会挂。
  ok('「最多同值」= px_ma60 轴里嵌套区间的个数（5 个）',
     FX.tune_rhythm.max_same_cap === 5, `max_same_cap=${FX.tune_rhythm.max_same_cap}`);

  // ---- 1. 两个容量轴 checkbox
  const axVals = $$('#axes input').map(i => i.value);
  ok('寻优轴含 rhythm', axVals.includes('rhythm'), axVals.join(','));
  ok('寻优轴含 pick', axVals.includes('pick'), axVals.join(','));
  ok('节奏轴默认不勾（否则会静默改变排序口径）',
     !$$('#axes input').find(i => i.value === 'rhythm').checked);

  // ---- 2. 档数说明来自后端（不写死在前端）
  const cands = FX.defaults.rhythm_cands || [];
  ok('fixture_defaults 含 rhythm_cands', cands.length === 10, `${cands.length} 条`);
  const eq = cands.filter(c => !c.ladder).length, lad = cands.length - eq;
  ok('节奏轴说明文字由 rhythm_cands 生成',
     $('#ax_rhythm_d').textContent.includes(`等权 ${eq} 档`) &&
     $('#ax_rhythm_d').textContent.includes(`阶梯 ${lad} 档`) &&
     $('#ax_rhythm_d').textContent.includes(`共 ${cands.length} 档`),
     $('#ax_rhythm_d').textContent.trim());

  // ---- 3. 口径提示的显隐
  pickAxes(['px_ma60']);
  ok('未勾容量轴 → 口径提示隐藏', $('#tune_cap_hint').style.display === 'none',
     `display="${$('#tune_cap_hint').style.display}"`);
  pickAxes(['px_ma60', 'rhythm']);
  ok('勾上节奏轴 → 口径提示出现', $('#tune_cap_hint').style.display === '',
     `display="${$('#tune_cap_hint').style.display}"`);

  /* ================================================================
     4. 信号口径（不限仓位）
     ================================================================ */
  pickAxes(['px_ma60', 'size_band', 'mkt_state']);
  click($('#tune_run'));
  await sleep(400);
  const sigHeads = heads();
  ok('信号口径：表头含 CAGR/MDD/Sharpe', ['CAGR', 'MDD', 'Sharpe'].every(h => sigHeads.includes(h)),
     sigHeads.join(' | '));
  ok('信号口径：表头含 胜率/PF/exw20/t20',
     ['胜率', 'PF', 'exw20', 't20'].every(h => sigHeads.includes(h)), sigHeads.join(' | '));
  ok('信号口径：**没有**账户口径列', !sigHeads.some(h => h.includes('账户')),
     sigHeads.filter(h => h.includes('账户')).join(',') || '无');
  const sigTxt = paneTxt();
  ok('信号口径：标注「不限仓位（研究报告）」', /不限仓位（研究报告）/.test(sigTxt));
  ok('信号口径：排序依据 = 不限仓位 Sharpe',
     /排序依据\s*不限仓位 Sharpe/.test(sigTxt),
     (sigTxt.match(/排序依据[^耗]*/) || ['（未找到）'])[0].slice(0, 40));
  const sigReq = reqs.filter(r => r.url.includes('/api/tune')).pop();
  ok('信号口径：请求体 axes 不含 rhythm',
     !sigReq.payload.axes.rhythm, JSON.stringify(sigReq.payload.axes));
  ok('信号口径：不出现「多行同结果」提示', !/种不同的账户净值/.test(sigTxt));

  /* ================================================================
     5. 容量口径（建仓节奏）
     ================================================================ */
  pickAxes(['rhythm']);
  click($('#tune_run'));
  await sleep(400);
  const capHeads = heads();
  ok('容量口径：表头含 账户CAGR/账户Sharpe/账户MDD',
     ['账户CAGR', '账户Sharpe', '账户MDD'].every(h => capHeads.includes(h)),
     capHeads.join(' | '));
  ok('容量口径：表头含 已投CAGR/平均持仓/建仓率',
     ['已投CAGR', '平均持仓', '建仓率'].every(h => capHeads.includes(h)),
     capHeads.join(' | '));
  ok('容量口径：**不再出现**胜率/PF（两套口径不混排）',
     !capHeads.includes('胜率') && !capHeads.includes('PF'),
     capHeads.filter(h => h === '胜率' || h === 'PF').join(',') || '无');
  const capTxt = paneTxt();
  ok('容量口径：标注「容量约束（实盘可执行）」', /容量约束（实盘可执行）/.test(capTxt));
  ok('容量口径：排序依据 = 账户资金口径 Sharpe',
     /排序依据\s*账户资金口径 Sharpe/.test(capTxt),
     (capTxt.match(/排序依据[^耗]*/) || ['（未找到）'])[0].slice(0, 44));
  const capReq = reqs.filter(r => r.url.includes('/api/tune')).pop();
  ok('容量口径：请求体 axes.rhythm === true', capReq.payload.axes.rhythm === true,
     JSON.stringify(capReq.payload.axes));
  ok('容量口径：行标签带节奏名（如「阶梯 1/1/2/3/3」）',
     /阶梯\s*1\/1\/2\/3\/3/.test(capTxt),
     (capTxt.match(/阶梯[^｜]{0,20}/) || ['（未找到）'])[0]);
  // 7. 「多行同结果」必须被解释（fixture 里 distinct < combo）
  ok('容量口径：出现「多行同结果」解释', /种不同的账户净值/.test(capTxt),
     (capTxt.match(/.{0,26}种不同的账户净值/) || ['（未找到）'])[0]);
  ok('「多行同结果」解释里点出成因（先从最窄档挑 / 多买不到）',
     /最窄/.test(capTxt) && /多买不到/.test(capTxt));
  ok('「多行同结果」解释里给出计数（最多 N 个组合撞在一起）',
     new RegExp(`最多\\s*${FX.tune_rhythm.max_same_cap}\\s*个组合撞在一起`).test(capTxt),
     (capTxt.match(/最多\s*\d+\s*个组合撞在一起/) || ['（未找到）'])[0]);

  /* ================================================================
     8. 表头点击换排序（账户CAGR）
     ================================================================ */
  const colVals = () => bodyRows().map(tr => parseFloat(cell(tr, '账户CAGR')));
  const headOf = kw => $$('#tune-tblwrap th').find(t => t.textContent.includes(kw));
  const nonIncreasing = a => a.every((v, i) => i === 0 || a[i - 1] >= v);
  const nonDecreasing = a => a.every((v, i) => i === 0 || a[i - 1] <= v);
  // 初始排序依据是 cap_sharpe，不是 cap_cagr —— 所以两列的顺序**不应该**恰好一致，
  // 否则说明「点表头」压根没生效（这正是本组断言要防的）。
  const sharpeVals = bodyRows().map(tr => parseFloat(cell(tr, '账户Sharpe')));
  ok('初始顺序按账户 Sharpe 降序（不是 CAGR）', nonIncreasing(sharpeVals),
     sharpeVals.slice(0, 5).join(' ≥ '));
  click(headOf('账户CAGR'));
  await sleep(30);
  const v1 = colVals();
  ok('点「账户CAGR」→ 整列降序', nonIncreasing(v1), v1.slice(0, 5).join(' ≥ '));
  ok('表头出现降序箭头 ▼', /▼/.test(headOf('账户CAGR').textContent),
     headOf('账户CAGR').textContent);
  click(headOf('账户CAGR'));
  await sleep(30);
  const v2 = colVals();
  ok('再点一次 → 整列升序', nonDecreasing(v2), v2.slice(0, 5).join(' ≤ '));
  ok('排序后行数不变（仍 40 行上限内）',
     bodyRows().length === Math.min(40, FX.tune_rhythm.rows.length),
     `${bodyRows().length} 行`);
  // 「越小越好」的指标首次点击应当按升序（否则点一下就得到最差的一批）
  click(headOf('账户MDD'));
  await sleep(30);
  const mdds = bodyRows().map(tr => parseFloat(cell(tr, '账户MDD')));
  ok('「账户MDD」首次点击按升序（回撤最小的排第一）', nonDecreasing(mdds),
     mdds.slice(0, 5).join(' ≤ '));

  // ---- 9. 无错误
  ok('无运行时错误', errors.length === 0, errors.slice(0, 3).join(' | ') || '干净');

  console.log('\n══════ 前端「参数寻优 · 建仓节奏」UI 验证 ══════\n');
  out.forEach(l => console.log(l));
  const pass = out.filter(l => l.startsWith('✅')).length;
  console.log(`\n通过 ${pass}/${out.length}`);
  process.exit(0);
}, 2600);
