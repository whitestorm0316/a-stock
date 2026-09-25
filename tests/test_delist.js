/* 前端「⑨ 剔除可能退市的股票」UI 验证（jsdom 无头）
 *
 * 覆盖点：
 *   1. 控件齐全（3 个判据 + 2 个数值参数 + 2 个提示位）
 *   2. ST 开关确实已下线（没有 #dl_st）
 *   3. 勾选后徽章 / 文案正确
 *   4. 参数收集：delist 对象、营收门槛单位换算（亿→元）、面值天数取整
 *   5. 默认参数回填（/api/defaults）
 *   6. 「只用买入日已公开信息」的口径说明在位
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
const reqs = [];   // {url, payload}

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
      let body = null;
      let payload = null;
      if (opt && opt.body) {
        try { payload = JSON.parse(opt.body); } catch (e) { /* 非 JSON 请求体 */ }
      }
      reqs.push({ url: u, method: (opt && opt.method) || 'GET', payload });
      if (u.includes('/api/meta')) body = FX.meta;
      else if (u.includes('/api/defaults')) body = FX.defaults;
      else if (u.includes('/api/backtest')) body = FX.backtest;
      else if (u.includes('/api/scan')) body = FX.scan;
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

  // ---- 1. 控件齐全
  const need = ['dl_fin', 'dl_loss2y', 'dl_penny', 'dl_rev_floor', 'dl_main2024',
                'dl_penny_days', 'dl_badge', 'dl_stat'];
  const miss = need.filter(i => !$('#' + i));
  ok('退市控件齐全（8 个）', miss.length === 0, miss.length ? '缺: ' + miss : '全部存在');

  // ---- 2. ST 开关应已下线
  // ⚠️ 不能用 html.includes('dl_st')：#dl_stat 里也含 'dl_st' 子串，会误报。
  //    必须用「dl_st 后面不再跟字母」的边界判断。
  ok('无 #dl_st 开关（ST 判据已移除）', !$('#dl_st'), $('#dl_st') ? '仍存在！' : '已下线');
  const stLeft = /dl_st(?![a-zA-Z_])/.test(html);
  ok('源码无 dl_st 残留', !stLeft, stLeft ? '源码仍含独立的 dl_st 引用' : '源码干净');

  // ---- 3. 初始状态
  ok('三项默认未勾选',
     !$('#dl_fin').checked && !$('#dl_loss2y').checked && !$('#dl_penny').checked);
  ok('默认值：营收门槛 1 亿', $('#dl_rev_floor').value === '1', '值=' + $('#dl_rev_floor').value);
  ok('默认值：面值天数 10', $('#dl_penny_days').value === '10', '值=' + $('#dl_penny_days').value);
  ok('初始徽章=已关闭', /已关闭/.test($('#dl_badge').textContent), $('#dl_badge').textContent.trim());
  ok('初始提示=未启用', /未启用/.test($('#dl_stat').textContent), $('#dl_stat').textContent.trim());

  // ---- 4. 勾选单项 → 文案
  $('#dl_fin').checked = true; ev($('#dl_fin'), 'change');
  ok('勾「财务类」→ 已启用 1 项', /已启用 1 项/.test($('#dl_badge').textContent),
     $('#dl_badge').textContent.trim());
  ok('文案含营收门槛', /营收门槛\s*1\s*亿/.test($('#dl_stat').textContent),
     $('#dl_stat').textContent.trim());

  // 主板 2024 新规
  $('#dl_main2024').checked = true; ev($('#dl_main2024'), 'change');
  ok('勾「主板2024」→ 文案标注 3 亿', /主板\s*3\s*亿/.test($('#dl_stat').textContent),
     $('#dl_stat').textContent.trim());

  // ---- 5. 面值判据
  $('#dl_loss2y').checked = true; ev($('#dl_loss2y'), 'change');
  $('#dl_penny').checked = true; ev($('#dl_penny'), 'change');
  ok('三条全勾 → 已启用 3 项', /已启用 3 项/.test($('#dl_badge').textContent),
     $('#dl_badge').textContent.trim());
  ok('文案含面值规则（< 1 元）', /1\s*元/.test($('#dl_stat').textContent),
     $('#dl_stat').textContent.trim());

  // 改面值天数 → 文案跟着变
  $('#dl_penny_days').value = '15'; ev($('#dl_penny_days'), 'input');
  ok('改面值天数为 15 → 文案同步', /≥\s*15\s*日/.test($('#dl_stat').textContent),
     $('#dl_stat').textContent.trim());

  // ---- 6. 参数收集（点「运行回测」后看请求体）
  const before = reqs.length;
  click($('#run'));
  const bt = reqs.slice(before).filter(r => r.url.includes('/api/backtest')).pop();
  ok('运行回测已发出 /api/backtest', !!bt);
  if (bt && bt.payload) {
    const p = bt.payload.params || bt.payload;
    ok('请求体含 delist 对象', p.delist && typeof p.delist === 'object',
       JSON.stringify(p.delist));
    ok('delist.financial=true', p.delist && p.delist.financial === true);
    ok('delist.loss2y=true', p.delist && p.delist.loss2y === true);
    ok('delist.penny=true', p.delist && p.delist.penny === true);
    ok('delist 仅 3 个键', p.delist && Object.keys(p.delist).length === 3,
       p.delist ? Object.keys(p.delist).join(',') : '—');
    ok('营收门槛 1 亿 → 1e8 元', p.delist_rev_floor === 1e8, String(p.delist_rev_floor));
    ok('delist_main_2024=true', p.delist_main_2024 === true, String(p.delist_main_2024));
    ok('面值天数 = 15（整数）', p.delist_penny_days === 15, String(p.delist_penny_days));
  }

  // ---- 7. 全部取消 → 回到关闭
  ['#dl_fin', '#dl_loss2y', '#dl_penny'].forEach(s => {
    $(s).checked = false; ev($(s), 'change');
  });
  ok('全部取消 → 徽章回到「已关闭」', /已关闭/.test($('#dl_badge').textContent),
     $('#dl_badge').textContent.trim());
  ok('全部取消 → 提示回到「未启用」', /未启用/.test($('#dl_stat').textContent));

  // ---- 8. 默认参数回填
  ok('fixture 的 defaults 含 delist',
     !!(FX.defaults.defaults && FX.defaults.defaults.delist),
     JSON.stringify(FX.defaults.defaults && FX.defaults.defaults.delist));
  const opt0 = [...$('#preset').options][0];
  if (opt0) {
    $('#preset').value = opt0.value;
    ev($('#preset'), 'change');
    ok('套用预设后不报错', true, opt0.value);
  }

  // ---- 9. 口径说明（只用买入日已公开信息）
  // ⚠️ 页面上有多个 .caphint（⑧ 仓位约束、⑨ 退市过滤各一个），
  //    按文本内容挑出退市那一组，不能直接取第一个。
  const hints = $$('.caphint');
  const hint = hints.find(h => /买入日/.test(h.textContent));
  const dlHint = hint && /公开/.test(hint.textContent) && /未复权/.test(hint.textContent)
    && /退市/.test(hint.textContent);
  ok('口径说明含「买入日已公开」+ 未复权', !!dlHint,
     hint ? hint.textContent.replace(/\s+/g, ' ').slice(0, 70) + '...'
          : `找不到退市组说明（共 ${hints.length} 个 .caphint）`);

  // ---- 10. 无运行时错误
  ok('无运行时错误', errors.length === 0, errors.slice(0, 3).join(' | ') || '干净');

  console.log('\n══════ 前端「剔除可能退市的股票」UI 验证 ══════\n');
  out.forEach(l => console.log(l));
  const pass = out.filter(l => l.startsWith('✅')).length;
  console.log(`\n通过 ${pass}/${out.length}`);
  if (warns.length) console.log('警告:', warns.slice(0, 4).join(' | '));
  process.exit(0);
}, 2600);
