/* 前端「行业两级树（门类 → 细分行业）」UI 验证（jsdom 无头）
 *
 * 背景：数据源只有同花顺 88 个细分行业，没有门类字段；门类是后端按
 *       app/industry_tree.json 做的人工归并。本套件守住三件事：
 *         1. 两级结构渲染正确（门类行 / 细分行业行 / 无细分行业的门类）
 *         2. 门类复选框的三态语义（全选 / 半选 / 未选）
 *         3. **请求体契约不变** —— pool.industries 必须仍然只发细分行业名，
 *            绝不能把门类代码（A/B/C…）混进去，否则后端过滤会静默失效
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
const chartCalls = [];
const reqLog = [];

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
            const s = o.series || [];
            rec.sizes = s.map(x => (x.data ? x.data.length : 0));
          },
          resize() {}, dispose() { rec.disposed = true; }, getDom: () => el,
        };
      },
    };
    w.fetch = async (url, opt) => {
      const u = String(url);
      let payload = {};
      if (opt && opt.body) { try { payload = JSON.parse(opt.body); } catch (e) { payload = {}; } }
      let body = null;
      if (u.includes('/api/meta')) body = FX.meta;
      else if (u.includes('/api/defaults')) body = FX.defaults;
      else if (u.includes('/api/backtest')) { body = FX.backtest; reqLog.push({ url: u, body: payload }); }
      else if (u.includes('/api/scan')) { body = FX.scan; reqLog.push({ url: u, body: payload }); }
      else if (u.includes('/api/tune')) body = { results: [], best: null, n: 0, ms: 0 };
      else if (u.includes('/api/stock')) body = { code: '000001.SZ', name: 'T', ind: 'I', rows: [], signal_dates: [] };
      else if (u.includes('/api/export')) body = new Uint8Array([1, 2, 3]);
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

  const TREE = FX.meta.industry_tree || [];
  const ABSENT = FX.meta.industry_absent || [];
  const UNL = FX.meta.industry_unlisted || [];
  // 面板里必然有「未知」（个股缺行业映射的兜底值），会被放进「未归类」兜底组，
  // 所以复选框总数 = 已归类细分行业数 + 兜底项数。
  const N_SUB = TREE.reduce((s, g) => s + g.n_sub, 0) + UNL.length;

  // ---- 0. fixture 前提
  ok('fixture_meta 含 industry_tree', TREE.length > 0,
     TREE.length ? `${TREE.length} 个门类` : '需先刷新 tests/fixtures/fixture_meta.json');

  // ---- 1. 结构
  const grps = $$('#inds .igt');
  const absRows = $$('#inds .iga');
  const ibx = $$('#inds input.ibx');
  const igc = $$('#inds input.igc');
  ok('门类组数 = 配置门类 + 未归类兜底组',
     grps.length === TREE.length + (UNL.length ? 1 : 0),
     `${grps.length} vs ${TREE.length}+${UNL.length ? 1 : 0}`);
  ok('无细分行业的门类单独列出', absRows.length === ABSENT.length,
     `${absRows.length} 行（${ABSENT.map(a => a.code).join('/')}）`);
  ok(`细分行业复选框总数 = ${N_SUB}`, ibx.length === N_SUB,
     `${ibx.length} vs ${N_SUB}（已归类 ${TREE.reduce((s, g) => s + g.n_sub, 0)} + 兜底 ${UNL.length}）`);
  ok('门类级复选框数 = 门类组数', igc.length === grps.length,
     `${igc.length} vs ${grps.length}`);
  ok('门类复选框在 igt 内且带 data-grp',
     igc.every(i => i.dataset.grp && i.closest('.igt')),
     igc.map(i => i.dataset.grp).join(','));
  // 关键：门类复选框不能带 value 属性，否则会被当成细分行业混进请求体
  ok('门类复选框不带 value 属性', igc.every(i => !i.hasAttribute('value')));

  // ---- 1b. 兜底组必须存在（否则「未知」这类行业会静默消失）
  if (UNL.length) {
    const fb = grps.find(g => g.dataset.code === '—');
    ok('存在「未归类」兜底组', !!fb, fb ? fb.querySelector('.ignm').textContent.trim() : '缺失');
    ok('兜底组内容与 industry_unlisted 一致',
       !!fb && [...fb.querySelectorAll('input.ibx')].map(i => i.value).sort().join() === [...UNL].sort().join(),
       fb ? [...fb.querySelectorAll('input.ibx')].map(i => i.value).join(',') : '—');
    // 兜底组的股票数是「数据缺失量级」的可见提示，后端给了就要显示出来
    const un = FX.meta.industry_unlisted_n;
    if (fb && un != null) {
      const cnt = fb.querySelector('.igcnt').textContent;
      ok('兜底组显示股票数', cnt.indexOf(String(un)) >= 0, cnt.trim());
    }
  }

  // ---- 2. 门类 C（制造业）应最大，且与 fixture 自洽
  const c = grps.find(g => g.dataset.code === 'C');
  const CTree = TREE.find(g => g.code === 'C');
  ok('存在门类 C 制造业', !!c && !!CTree, CTree ? `${CTree.n_sub} 细分行业` : '缺失');
  if (c && CTree) {
    ok('C 组细分行业数与 fixture 一致',
       c.querySelectorAll('input.ibx').length === CTree.n_sub,
       `${c.querySelectorAll('input.ibx').length} vs ${CTree.n_sub}`);
    const nm = c.querySelector('.ignm').textContent.trim();
    ok('C 组名称正确', nm === '制造业', nm);
  }
  // 88 个细分行业应无重复
  const vals = ibx.map(i => i.value);
  ok('细分行业无重复', new Set(vals).size === vals.length,
     `${new Set(vals).size} / ${vals.length}`);

  // ---- 3. 三态语义
  const cBox = c.querySelector('input.igc');
  const cSubs = [...c.querySelectorAll('input.ibx')];
  ok('初始全部未选', !cBox.checked && !cBox.indeterminate);
  cSubs[0].checked = true; ev(cSubs[0], 'change');
  ok('勾 1 个子项 → 门类半选(indeterminate)',
     !cBox.checked && cBox.indeterminate === true,
     `checked=${cBox.checked} indet=${cBox.indeterminate}`);
  cSubs.forEach(i => { i.checked = true; }); ev(cSubs[0], 'change');
  ok('全勾 → 门类实选', cBox.checked === true && cBox.indeterminate === false);
  cSubs.forEach(i => { i.checked = false; }); ev(cSubs[0], 'change');
  ok('全不勾 → 门类未选', cBox.checked === false && cBox.indeterminate === false);

  // ---- 4. 点门类复选框 = 全选本组
  cBox.checked = true; ev(cBox, 'change');
  ok('点门类复选框 → 本组子项全选',
     cSubs.every(i => i.checked), `已选 ${cSubs.filter(i => i.checked).length}/${cSubs.length}`);
  ok('只影响本组', $$('#inds input.ibx:checked').length === cSubs.length,
     `全局已选 ${$$('#inds input.ibx:checked').length}`);
  cBox.checked = false; ev(cBox, 'change');
  ok('再点门类复选框 → 本组全不选',
     cSubs.every(i => !i.checked) && $$('#inds input.ibx:checked').length === 0);

  // ---- 5. 展开/收起
  ok('默认收起', !c.classList.contains('indopen'));
  click(c.querySelector('.igh'));
  ok('点门类名称行 → 展开', c.classList.contains('indopen'));
  click(c.querySelector('.igh'));
  ok('再点 → 收起', !c.classList.contains('indopen'));
  click(cBox);
  ok('点复选框不触发展开切换', !c.classList.contains('indopen'),
     c.classList.contains('indopen') ? '被误切换' : '正确');
  const expBtn = $$('.poolcls button').find(b => b.dataset.act === 'expandind');
  ok('存在「展开全部」按钮', !!expBtn);
  if (expBtn) {
    click(expBtn);
    ok('展开全部 → 所有门类展开',
       grps.every(g => g.classList.contains('indopen')));
    click(expBtn);
    ok('再点 → 全部收起', grps.every(g => !g.classList.contains('indopen')));
  }

  // ---- 6. 计数与全局全选/清空
  const allBtn = $$('.poolcls button').find(b => b.dataset.act === 'allind');
  const noneBtn = $$('.poolcls button').find(b => b.dataset.act === 'noneind');
  click(allBtn);
  ok(`全选 → ${N_SUB} 个细分行业全勾`, $$('#inds input.ibx:checked').length === N_SUB,
     String($$('#inds input.ibx:checked').length));
  ok('全选 → 计数文案', new RegExp(`选中 ${N_SUB} 个`).test($('#indcnt').textContent),
     $('#indcnt').textContent.trim());
  ok('全选 → 所有门类复选框实选',
     $$('#inds input.igc').every(i => i.checked && !i.indeterminate));
  click(noneBtn);
  ok('清空 → 归零', $$('#inds input.ibx:checked').length === 0);
  ok('清空 → 计数回默认', /0 选中 = 不限/.test($('#indcnt').textContent),
     $('#indcnt').textContent.trim());

  // ---- 7. 单个行业勾选时计数应实时更新（原实现漏挂监听的 bug）
  const a = grps.find(g => g.dataset.code === 'A');
  const aSub = a.querySelector('input.ibx');
  if (aSub) {
    aSub.checked = true; ev(aSub, 'change');
    ok('单勾一个细分行业 → 计数实时更新',
       /选中 1 个/.test($('#indcnt').textContent), $('#indcnt').textContent.trim());
    aSub.checked = false; ev(aSub, 'change');
  }

  // ---- 8. 搜索
  const q = $('#ind_q');
  ok('存在行业搜索框', !!q);
  if (q) {
    // 搜索「半导体」→ 只应命中 1 个细分行业
    q.value = '半导体'; ev(q, 'input');
    const vis = $$('#inds .ibx').filter(i => !i.closest('label').hidden);
    ok('搜索「半导体」→ 命中 1 项', vis.length === 1,
       `${vis.length} 项：${vis.map(i => i.value).join(',')}`);
    ok('搜索 → 非命中门类被隐藏',
       $$('#inds .igt').filter(g => !g.hidden).length === 1,
       `可见 ${$$('#inds .igt').filter(g => !g.hidden).length} 个门类`);
    const shown = $$('#inds .igt').filter(g => !g.hidden);
    ok('搜索 → 命中门类自动展开',
       shown.length > 0 && shown.every(g => g.classList.contains('indopen')),
       `可见 ${shown.length} 个门类，展开 ${shown.filter(g => g.classList.contains('indopen')).length} 个`);
    // 大小写不敏感（IT服务 含拉丁字母）
    q.value = 'it服务'; ev(q, 'input');
    const vis2 = $$('#inds .ibx').filter(i => !i.closest('label').hidden);
    ok('搜索大小写不敏感（it服务 → IT服务）',
       vis2.length === 1 && vis2[0].value === 'IT服务',
       vis2.map(i => i.value).join(',') || '无命中');
    // 清空 → 全部恢复
    q.value = ''; ev(q, 'input');
    ok('清空搜索 → 全部细分行业恢复可见',
       $$('#inds .ibx').filter(i => !i.closest('label').hidden).length === N_SUB,
       String($$('#inds .ibx').filter(i => !i.closest('label').hidden).length));
    ok('清空搜索 → 所有门类恢复可见',
       $$('#inds .igt').every(g => !g.hidden));
    const clr = $('#ind_q_clr');
    if (clr) {
      q.value = '银行'; ev(q, 'input');
      click(clr);
      ok('清空按钮 → 输入框复位',
         q.value === '' && $$('#inds .igt').every(g => !g.hidden));
    }
  }

  // ---- 9. ★ 请求体契约：pool.industries 只能含细分行业名
  const before = reqLog.length;
  // 清空环境后精确选两项
  click(noneBtn);
  const pick = ['半导体', '银行'];
  pick.forEach(v => {
    const bx = $$('#inds input.ibx').find(i => i.value === v);
    if (bx) { bx.checked = true; ev(bx, 'change'); }
  });
  click($('#run'));
  const hit = reqLog.slice(before).reverse().find(r => (r.body.pool || {}).industries);
  ok('已发出带 pool 的请求', !!hit);
  if (hit) {
    const inds = hit.body.pool.industries;
    ok('pool.industries 是数组', Array.isArray(inds), JSON.stringify(inds));
    ok('pool.industries 恰为所选 2 项',
       inds.length === 2 && pick.every(p => inds.includes(p)), JSON.stringify(inds));
    // ★ 最关键：绝不能混入门类代码
    const codes = (FX.meta.industry_tree || []).map(g => g.code);
    const bad = inds.filter(x => codes.includes(x));
    ok('pool.industries 不含门类代码（A/B/C…）', bad.length === 0,
       bad.length ? '混入: ' + bad.join(',') : '干净');
    ok('pool.industries 与「全选」无关', inds.length < N_SUB,
       `${inds.length} 项`);
  }

  // ---- 10. 无运行时错误
  ok('无运行时错误', errors.length === 0, errors.slice(0, 3).join(' | ') || '干净');

  console.log('\n══════ 前端「行业两级树」UI 验证 ══════\n');
  out.forEach(l => console.log(l));
  const pass = out.filter(l => l.startsWith('✅')).length;
  console.log(`\n通过 ${pass}/${out.length}`);
  if (warns.length) console.log('警告:', warns.slice(0, 4).join(' | '));
  process.exit(0);
}, 2600);
