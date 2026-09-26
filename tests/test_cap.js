/* 前端「仓位约束（实盘可执行性）」验证（jsdom 无头）
 *
 * 覆盖：
 *   1. ⑧ 仓位约束 面板存在；默认口径 = 不限仓位
 *   2. collect() 不限仓位时 max_pos=0；切到「限制持仓」后带出 max_pos/max_new/pick
 *   3. 快捷预设按钮（10只/日3只 等）正确写入并自动切换到「限制持仓」
 *   4. pick 下拉由 /api/defaults 的 pick_rules 填充，默认选中 deep
 *   5. 🔴 默认（不限仓位）时「先填充下拉再 applyParams」的顺序正确 ——
 *      否则 cap_pick 还没有 option，选不中（本测试专门防回归）
 *   6. 结果渲染：capacity=null 时给出「去左侧开启」的引导，不报错
 *   7. 结果渲染：capacity 有值时双口径卡片出现，且「账户资金口径」用 cap_cagr
 *   8. 结果渲染：受限净值曲线加入净值图（nav_cap）
 *   9. 逐年图/IS-OOS 表带出受限口径对比行
 *  10. 重置按钮把口径恢复为「不限仓位」
 *  11. 无运行时错误
 */
const fs = require('fs');
// jsdom 从项目根 node_modules 解析（见下方 ROOT）

const path = require('path');
const ROOT = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'app/index.html'), 'utf8');
const { JSDOM } = require(require('path').join(ROOT, 'node_modules/jsdom'));
const F = n => JSON.parse(fs.readFileSync(path.join(__dirname, `fixtures/fixture_${n}.json`), 'utf8'));

let pass = 0, fail = 0;
const ok = (c, m) => { if (c) { pass++; } else { fail++; console.log('  ✗ ' + m); } };

const errors = [];
const reqLog = [];

function boot(useCap) {
  const FX = {
    meta: F('meta'), defaults: F('defaults'),
    backtest: useCap ? F('backtest_cap') : F('backtest'),
    scan: F('scan'), trades: F('trades')
  };
  const chartOpts = {};   // id → 最后一次 setOption 的内容
  const dom = new JSDOM(html, {
    runScripts: 'dangerously', pretendToBeVisual: true,
    url: 'http://127.0.0.1:8771/',
    beforeParse(w) {
      w.HTMLElement.prototype.scrollIntoView = () => {};
      w.requestAnimationFrame = cb => setTimeout(() => cb(Date.now()), 0);
      w.echarts = {
        init: el => {
          const id = el && el.id;
          return { setOption(o) { if (id) chartOpts[id] = o; },
                   resize() {}, dispose() {}, getDom: () => el };
        }
      };
      w.fetch = async (url, opt) => {
        const u = String(url);
        const payload = opt && opt.body ? JSON.parse(opt.body) : {};
        let body = {};
        if (u.includes('/api/meta')) body = FX.meta;
        else if (u.includes('/api/defaults')) body = FX.defaults;
        else if (u.includes('/api/backtest')) { body = FX.backtest; reqLog.push({ url: u, body: payload }); }
        else if (u.includes('/api/scan')) { body = FX.scan; reqLog.push({ url: u, body: payload }); }
        else if (u.includes('/api/trades')) body = FX.trades;
        else if (u.includes('/api/export')) body = new Uint8Array([1]);
        return { ok: true, status: 200, headers: { get: () => null },
          json: async () => body, text: async () => JSON.stringify(body),
          blob: async () => ({ size: 1 }), arrayBuffer: async () => new ArrayBuffer(1) };
      };
      w.URL.createObjectURL = () => 'blob:x';
      w.URL.revokeObjectURL = () => {};
      w.onerror = (m, s, l, c) => errors.push(`${m} @${l}:${c}`);
      w.console.error = (...a) => errors.push(a.join(' '));
    }
  });
  return { dom, w: dom.window, d: dom.window.document, chartOpts };
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  // ============ A. 不限仓位（默认） ============
  let { w, d } = boot(false);
  await sleep(500);

  console.log('【A】不限仓位（默认口径）');
  ok(!!d.querySelector('#seg-cap'), '⑧ 仓位约束 的 seg-cap 应存在');
  ok(!!d.querySelector('#cap_maxpos'), '同时持仓输入框应存在');
  ok(!!d.querySelector('#cap_maxnew'), '每日买入输入框应存在');
  ok(!!d.querySelector('#cap_pick'), '选股规则下拉应存在');
  ok(!!d.querySelector('#capwrap'), '容量参数包裹层应存在');

  const onBtn = d.querySelector('#seg-cap button.on');
  ok(onBtn && onBtn.dataset.v === '0', `默认口径应为「不限仓位」，实际 ${onBtn && onBtn.dataset.v}`);

  // pick 下拉是否被 pick_rules 填充（关键：初始化顺序）
  const opts = d.querySelectorAll('#cap_pick option');
  ok(opts.length === 7, `pick 下拉应有 7 个选项（来自 pick_rules），实际 ${opts.length}`);
  ok(d.querySelector('#cap_pick').value === 'deep',
     `pick 默认应选中 deep，实际 ${d.querySelector('#cap_pick').value}`);
  const optText = [...opts].map(o => o.textContent).join('|');
  ok(optText.includes('最超跌优先'), 'pick 选项应含「最超跌优先」');

  // 面板在「不限仓位」时应隐藏
  ok(d.querySelector('#capwrap').style.display === 'none',
     '不限仓位时容量参数区应隐藏');

  // 状态提示
  const stat0 = d.querySelector('#cap_stat').textContent;
  ok(stat0.includes('不限仓位'), `不限仓位提示文本应含「不限仓位」，实际「${stat0.slice(0,40)}」`);

  // max_pos=0 的请求体
  const bt0 = reqLog.filter(r => r.url.includes('/api/backtest')).pop();
  ok(bt0 && bt0.body.params.max_pos === 0,
     `不限仓位时请求体 max_pos 应为 0，实际 ${bt0 && bt0.body.params.max_pos}`);

  // 结果里 capacity=null → 引导文案
  const btHtml0 = d.querySelector('#p-bt').innerHTML;
  ok(btHtml0.includes('仓位约束'), '回测结果应含「仓位约束」卡片');
  ok(btHtml0.includes('943.9') || btHtml0.includes('不限仓位'),
     'capacity=null 时应给出不限仓位说明');

  // ============ B. 切到「限制持仓」 ============
  console.log('【B】切到限制持仓');
  const cap1 = [...d.querySelectorAll('#seg-cap button')].find(b => b.dataset.v === '1');
  cap1.dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await sleep(60);

  ok(d.querySelector('#seg-cap button.on').dataset.v === '1', '点击后应切到「限制持仓」');
  ok(d.querySelector('#capwrap').style.display === 'block', '限制持仓时参数区应显示');
  ok(d.querySelector('#capwrap').classList.contains('on'), '限制持仓时容器应加 on 样式类');
  const stat1 = d.querySelector('#cap_stat').textContent;
  ok(stat1.includes('限制持仓'), '状态提示应含「限制持仓」');
  ok(d.querySelector('#cap_badge').textContent.includes('10只'), '徽章应显示 10只/3只');

  // ============ C. 快捷预设 ============
  console.log('【C】快捷预设');
  const q20 = [...d.querySelectorAll('.capquick button')].find(b => b.dataset.cap === '20,3');
  q20.dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await sleep(60);
  ok(d.querySelector('#cap_maxpos').value === '20', '点 20只/日3只 后持仓上限应为 20');
  ok(d.querySelector('#cap_maxnew').value === '3', '点 20只/日3只 后每日买入应为 3');
  ok(d.querySelector('#seg-cap button.on').dataset.v === '1', '快捷预设应自动切到限制持仓');
  ok(q20.classList.contains('on'), '被点的快捷按钮应高亮');

  // 回落到 10,3 以便后续断言
  const q10 = [...d.querySelectorAll('.capquick button')].find(b => b.dataset.cap === '10,3');
  q10.dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await sleep(60);

  // 手动改数字应取消快捷高亮
  const inp = d.querySelector('#cap_maxpos');
  inp.value = '12';
  inp.dispatchEvent(new w.Event('input', { bubbles: true }));
  await sleep(40);
  ok(!d.querySelector('.capquick button.on'), '手动改数字后快捷按钮应取消高亮');
  inp.value = '10';
  inp.dispatchEvent(new w.Event('input', { bubbles: true }));
  await sleep(40);

  // pick 改成小市值优先，验证能带出去
  d.querySelector('#cap_pick').value = 'small';
  d.querySelector('#cap_pick').dispatchEvent(new w.Event('change', { bubbles: true }));
  await sleep(40);
  ok(d.querySelector('#cap_stat').textContent.includes('小市值优先'),
     '改 pick 后状态提示应同步');

  // 运行一次回测，检查请求体
  reqLog.length = 0;
  d.querySelector('#run').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await sleep(600);
  const bt1 = reqLog.filter(r => r.url.includes('/api/backtest')).pop();
  ok(bt1 && bt1.body.params.max_pos === 10,
     `限制持仓时请求体 max_pos 应为 10，实际 ${bt1 && bt1.body.params.max_pos}`);
  ok(bt1 && bt1.body.params.max_new === 3,
     `请求体 max_new 应为 3，实际 ${bt1 && bt1.body.params.max_new}`);
  ok(bt1 && bt1.body.params.pick === 'small',
     `请求体 pick 应为 small，实际 ${bt1 && bt1.body.params.pick}`);

  // ============ D. 容量结果渲染 ============
  console.log('【D】容量结果渲染（capacity 有值）');
  const { w: w2, d: d2, chartOpts } = boot(true);
  await sleep(700);

  // D 组用默认不限仓位启动，所以 capacity 有值但 UI 是"不限仓位"——
  // 真实场景是开了约束才有值；这里直接验证渲染函数对 capacity 的呈现能力。
  const h = d2.querySelector('#p-bt').innerHTML;
  ok(h.includes('仓位约束结果'), '应渲染「仓位约束结果」标题');
  ok(h.includes('已投资金口径'), '应含「已投资金口径」');
  ok(h.includes('账户资金口径'), '应含「账户资金口径」');

  // 数值全部**从 fixture 反推期望值**，不写死 —— 面板一更新数据，
  // cap_cagr / z 就会变，写死的断言每刷新一次 fixtures 就要手改一次。
  const cap = F('backtest_cap').capacity;
  const p1 = v => (v * 100).toFixed(1);
  ok(h.includes(p1(cap.cap_cagr)),
    `账户口径 CAGR 应出现 ${p1(cap.cap_cagr)}%，片段: ${h.match(/账户资金口径[\s\S]{0,200}/)?.slice(0,110)}`);
  ok(h.includes(p1(cap.cagr)), `已投资金口径 CAGR 应出现 ${p1(cap.cagr)}%`);
  ok(h.includes(String(cap.n_hold)), `实际建仓数 ${cap.n_hold} 应出现`);
  ok(h.includes(p1(cap.drop_pct)), `丢弃率 ${p1(cap.drop_pct)}% 应出现`);
  ok(h.includes(cap.avg_pos.toFixed(1)), `平均持仓 ${cap.avg_pos.toFixed(1)} 应出现`);
  ok(h.includes(cap.z.toFixed(2)), `z 值 ${cap.z.toFixed(2)} 应出现`);
  ok(h.includes(p1(cap.rand_pctile) + '%') || h.includes('100%'),
    `随机百分位 ${p1(cap.rand_pctile)}% 应出现`);
  ok(h.includes('显著优于随机'),
    `z=${cap.z.toFixed(2)}（>2）时应给出「显著优于随机」结论`);
  ok(h.includes('最超跌优先'), '应显示选股规则名');

  // 逐年图带受限系列
  const yo = chartOpts['ch-year'];
  ok(!!yo, '逐年图应被渲染');
  if (yo) {
    const names = (yo.series || []).map(s => s.name);
    ok(names.some(n => n && n.includes('受限')), `逐年图应有「受限」系列，实际 ${JSON.stringify(names)}`);
    ok(names.some(n => n === '不限仓位'), `逐年图应有「不限仓位」系列，实际 ${JSON.stringify(names)}`);
  }

  // 净值图带受限曲线
  const no = chartOpts['ch-nav'];
  ok(!!no, '净值图应被渲染');
  if (no) {
    const names = (no.series || []).map(s => s.name);
    ok(names.some(n => n && n.includes('受限仓位')),
       `净值图应有「受限仓位」曲线，实际 ${JSON.stringify(names)}`);
    const s = no.series.find(x => x.name && x.name.includes('受限仓位'));
    ok(s && s.data.length === no.xAxis.data.length,
       `受限净值曲线长度应等于 x 轴（${s && s.data.length} vs ${no.xAxis.data.length}）`);
  }

  // IS/OOS 表带受限行
  ok(h.includes('受限 10只/日3只'), 'IS/OOS 表应含受限口径行');

  // ============ E. 重置 ============
  console.log('【E】重置');
  d2.querySelector('#reset').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await sleep(700);
  ok(d2.querySelector('#seg-cap button.on').dataset.v === '0', '重置后应回到「不限仓位」');
  ok(d2.querySelector('#capwrap').style.display === 'none', '重置后参数区应隐藏');

  // ============ 错误 ============
  console.log('【F】运行时错误');
  const real = errors.filter(e => !/Not implemented|Could not parse CSS|jsdom/i.test(e));
  ok(real.length === 0, `不应有运行时错误，实际: ${real.slice(0, 3).join(' | ')}`);

  console.log(`\n${fail === 0 ? '✅' : '❌'}  仓位约束前端：${pass}/${pass + fail} 通过`);
  process.exit(fail === 0 ? 0 : 1);
})();
