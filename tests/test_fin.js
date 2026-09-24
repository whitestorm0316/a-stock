/* 前端财务筛选 UI 渲染验证（jsdom 无头） */
const fs = require('fs');
// jsdom 从项目根 node_modules 解析（见下方 ROOT）

const path = require('path');
const ROOT = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'app/index.html'), 'utf8');
const { JSDOM } = require(require('path').join(ROOT, 'node_modules/jsdom'));
const F = n => JSON.parse(fs.readFileSync(path.join(__dirname, `fixtures/fixture_${n}.json`), 'utf8'));
const FX = { meta: F('meta'), defaults: F('defaults'), backtest: F('backtest'), scan: F('scan') };

const errors = [], warns = [];
const chartCalls = [];
const fetchLog = [];

const dom = new JSDOM(html, {
  runScripts: 'dangerously',
  pretendToBeVisual: true,
  url: 'http://127.0.0.1:8771/',
  beforeParse(w) {
    w.HTMLElement.prototype.scrollIntoView = () => {};
    w.requestAnimationFrame = cb => setTimeout(() => cb(Date.now()), 0);
    // ECharts 桩
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
          resize() {}, dispose() { rec.disposed = true; },
          getDom: () => el,
        };
      },
    };
    // fetch 桩
    w.fetch = async (url, opt) => {
      const u = String(url);
      fetchLog.push((opt && opt.method) || 'GET');
      let body = null;
      if (u.includes('/api/meta')) body = FX.meta;
      else if (u.includes('/api/defaults')) body = FX.defaults;
      else if (u.includes('/api/backtest')) body = FX.backtest;
      else if (u.includes('/api/scan')) body = FX.scan;
      else if (u.includes('/api/tune')) body = { results: [], best: null, n: 0, ms: 0 };
      else if (u.includes('/api/stock')) body = { code: '000001.SZ', name: 'T', ind: 'I', rows: [], signal_dates: [] };
      else if (u.includes('/api/export')) body = new Uint8Array([1, 2, 3]);
      return { ok: true, status: 200, headers: { get: () => null },
        json: async () => body, text: async () => JSON.stringify(body),
        blob: async () => ({ size: 3 }), arrayBuffer: async () => new ArrayBuffer(3) };
    };
    w.URL.createObjectURL = () => 'blob:x';
    w.URL.revokeObjectURL = () => {};
    w.onerror = (m, s, l, c, e) => errors.push(`${m} @${l}:${c}`);
    const ce = w.console.error.bind(w.console);
    w.console.error = (...a) => { errors.push(a.join(' ')); };
    const cw = w.console.warn.bind(w.console);
    w.console.warn = (...a) => { warns.push(a.join(' ')); };
  },
});

const w = dom.window, d = w.document;
const $ = s => d.querySelector(s), $$ = s => [...d.querySelectorAll(s)];

setTimeout(() => {
  const out = [];
  const ok = (t, c, x) => out.push(`${c ? '✅' : '❌'} ${t}${x !== undefined ? '  → ' + x : ''}`);

  // ---- 1. 财务 UI 存在性
  const need = ['fin_rev_min', 'fin_rev_max', 'fin_ry_min', 'fin_ry_max',
                'fin_np_min', 'fin_np_max', 'fin_ny_min', 'fin_ny_max',
                'fin_period', 'fin_stat', 'fin_badge'];
  const miss = need.filter(i => !$('#' + i));
  ok('财务控件齐全（11 个）', miss.length === 0, miss.length ? '缺: ' + miss : '全部存在');

  // ---- 2. 动态说明文案
  const hint = $('.finhint');
  ok('动态口径说明存在', !!hint && /披露日/.test(hint.textContent) && /前视偏差/.test(hint.textContent));

  // ---- 3. 快捷按钮
  const qb = $$('.finquick button').map(b => b.dataset.fin);
  ok('财务快捷按钮 5 个', qb.length === 5, qb.join(','));

  // ---- 4. 初始状态
  ok('初始未启用财务', /未启用/.test($('#fin_stat').textContent), $('#fin_stat').textContent.trim());

  // ---- 5. 输入后徽章更新
  const ev = (el, t) => el.dispatchEvent(new w.Event(t, { bubbles: true }));
  $('#fin_ry_min').value = '20'; ev($('#fin_ry_min'), 'input');
  const s1 = $('#fin_stat').textContent;
  ok('输入后状态更新', /已启用/.test(s1) && /20/.test(s1), s1.trim());

  // ---- 6. 快捷按钮生效
  $('#fin_ry_min').value = ''; ev($('#fin_ry_min'), 'input');
  const rev20 = $$('.finquick button').find(b => b.dataset.fin === 'rev20');
  rev20.dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  ok('快捷「营收增20%+」写入', $('#fin_ry_min').value === '20', '值=' + $('#fin_ry_min').value);
  ok('快捷按钮高亮', rev20.classList.contains('on'));

  // ---- 7. 清空
  $$('.finquick button').find(b => b.dataset.fin === 'clear')
    .dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  ok('清空生效', $('#fin_ry_min').value === '' && /未启用/.test($('#fin_stat').textContent));

  // ---- 8. 预设套用（选 K3G20）
  const sel = $('#preset');
  const opt = [...sel.options].find(o => o.value === 'K3G20');
  ok('预设含 K3G20', !!opt);
  if (opt) {
    sel.value = 'K3G20';
    sel.dispatchEvent(new w.Event('change', { bubbles: true }));
    ok('套用 K3G20 后营收同比=20', $('#fin_ry_min').value === '20', '值=' + $('#fin_ry_min').value);    ok('套用后状态已启用', /已启用/.test($('#fin_stat').textContent), $('#fin_stat').textContent.trim());
  }

  // ---- 9. 候选表含财务列
  const ths = $$('#p-scan th').map(t => t.textContent.replace(/[▲▼]/g, '').trim());
  const fcol = ['营收', '营收同比', '归母净利', '归母同比', '财报期'];
  const missing = fcol.filter(c => !ths.includes(c));
  ok('候选表财务列（5 列）', missing.length === 0, missing.length ? '缺: ' + missing : ths.length + ' 列');
  ok('候选表含板块列', ths.includes('板块'), ths.filter(x => /板块|市值组/.test(x)).join(','));
  ok('表头列数 = 21', ths.length === 21, '实际 ' + ths.length);

  // ---- 10. 候选行渲染
  /* ⚠️ 列索引一律用表头名称查，不要写死数字。
     写死的话，每次往表里插一列都会连带弄坏所有断言（本轮加「板块」列就踩到了）。 */
  const col = name => ths.indexOf(name);
  const rows = $$('#scanbody tr');
  ok('候选行已渲染', rows.length > 0, rows.length + ' 行');
  if (rows.length) {
    const tds = rows[0].querySelectorAll('td');
    const txt = [...tds].map(t => t.textContent.trim());
    ok('首行 21 个单元格', tds.length === ths.length, `行 ${tds.length} vs 表头 ${ths.length}`);
    const money = txt[col('营收')], yoy = txt[col('营收同比')], fq = txt[col('财报期')];
    ok('营收列有值', /亿|万/.test(money) || money === '—', money);
    ok('营收同比列有值', /%/.test(yoy) || yoy === '—', yoy);
    ok('财报期列格式化', /-\d\d Q\d/.test(fq) || fq === '—', fq);
    // 板块列必须是中文标签而非 MAIN/CHINEXT 原始码
    const bd = txt[col('板块')];
    ok('板块列为中文标签', ['主板', '创业板', '科创板', '北交所', '—'].includes(bd), bd);
    // 市值组用 M 前缀（不是 D，避免与档位列混淆）
    const sg = txt[col('市值组')];
    ok('市值组列为 M+数字', /^M\d+$/.test(sg) || sg === '—', sg);
  }

  // ---- 11. 图表
  const empty = chartCalls.filter(c => c.sizes.length === 0 || c.sizes.every(s => !s));
  ok('图表全部非空', chartCalls.length > 0 && empty.length === 0,
     `${chartCalls.length} 张，空 ${empty.length}`);

  // ---- 12. 无错误
  ok('无运行时错误', errors.length === 0, errors.slice(0, 3).join(' | ') || '干净');

  console.log('\n══════ 前端财务筛选 UI 验证 ══════\n');
  out.forEach(l => console.log(l));
  const pass = out.filter(l => l.startsWith('✅')).length;
  console.log(`\n通过 ${pass}/${out.length}`);
  if (warns.length) console.log('警告:', warns.slice(0, 4).join(' | '));
  process.exit(0);
}, 2600);
