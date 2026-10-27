
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const F = (v, n = 1, suf = '') => (v === null || v === undefined || !isFinite(v))
  ? '—' : v.toFixed(n) + suf;
const PCT = (v, n = 1) => (v === null || v === undefined || !isFinite(v)) ? '—'
  : (v * 100).toFixed(n) + '%';
/* ⚠️ PCTN = 值本身已是百分数（如 8.53 表示 8.53%），直接加 % 号，切勿再 ×100。
   交易明细的 rows / best / worst 用这个（后端已乘 100）；
   summary / yearly / hist 里的字段是比率（0.0452），仍用 PCT。
   两套单位混在同一个接口里，最容易写错 —— 改这里前先看 engine.trades() 的注释。 */
const PCTN = (v, n = 2) => (v === null || v === undefined || !isFinite(v)) ? '—'
  : v.toFixed(n) + '%';
/* 板块代码 → 中文短标签（后端 /api/meta 的 board_names 同源，此处硬编码便于表格快速渲染）*/
const BD_MAP = { MAIN: '主板', CHINEXT: '创业板', STAR: '科创板', BJ: '北交所' };
const BD = v => BD_MAP[v] || (v || '—');
const cls = v => (v > 0 ? 'pos' : v < 0 ? 'neg' : 'neu');
const NF = new Intl.NumberFormat('zh-CN');
const NUM = v => (v === null || v === undefined) ? '—' : NF.format(Math.round(v));
/* 判空（含 NaN）—— 给「有值才渲染 / 否则换一种说法」的场景用。
   ⚠️ 不要写 `isFinite(v)`：`isFinite(null) === true`（null 会被转成 0），
      会把「后端没给这个字段」误判成「值等于 0」。必须显式排掉 null/undefined。 */
const hasNum = v => v !== null && v !== undefined && isFinite(v);
/* 金额（元）→ 亿/万 可读文本 */
const MONEY = v => {
  if (v === null || v === undefined || !isFinite(v)) return '—';
  const a = Math.abs(v);
  if (a >= 1e8) return (v / 1e8).toFixed(1) + '亿';
  if (a >= 1e4) return (v / 1e4).toFixed(0) + '万';
  return v.toFixed(0);
};

let META = null, LAST = null, DEFAULT_PARAMS = {}, PRESETS = [], USER_PRESETS = [];
// 界面默认选中的方案 id（'' = 未设置 → 回退到内置第一套）。来自 /api/defaults，
// 由「设为默认 / 取消默认」按钮改。⚠️ 可能指向已不存在的 id，用前必须校验。
let DEFAULT_PRESET = '';
// 市值分位：序号 0..9（每档 10%），0 = 最小；标签与后端 SIZE_PCT 同源（init 时用
// /api/defaults 的 size_bands 覆盖，避免两端各写一份百分比表而对不上）
let SIZE_PCT = ['10%', '20%', '30%', '40%', '50%', '60%', '70%', '80%', '90%', '100%'];
const SIZE_N = 10;
let POOL = { mode: 'all', codes_text: '', industries: [], boards: ['MAIN', 'CHINEXT', 'STAR'], exchanges: ['SH', 'SZ'] };
let CFG = {};          // 当前参数
let SCAN = null;       // 最近一次扫描结果
const charts = {};

/* ---------------- 网络 ---------------- */
async function api(path, body, isGet) {
  const opt = isGet ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {})
  };
  const r = await fetch(path, opt);
  const txt = await r.text();
  let j; try { j = JSON.parse(txt); } catch (e) { throw new Error('返回非 JSON：' + txt.slice(0, 200)); }
  if (!r.ok && j.error) throw new Error(j.error);
  if (j.error) throw new Error(j.error);
  return j;
}

/* ---------------- 参数收集 ---------------- */
function segVal(id) {
  const b = $(id + ' button.on');
  return b ? { lo: b.dataset.lo, hi: b.dataset.hi, v: b.dataset.v } : null;
}
function segv(p) { const b = $(p + ' button.on'); return b ? b.dataset.v : null; }
function nz(x) {
  if (x === null || x === undefined || x === '') return null;
  const n = parseInt(x, 10); return isNaN(n) ? null : n;
}

/* ---------------- 市值区间（双滑块） ----------------
 * 语义与后端 norm_size_band / size_band_label 严格对齐：
 *   序号 0..9，0 = 最小 10%，9 = 最大 10%；区间为闭区间。
 *   「10%~20%」= 只取第 2 个分位 → smin = smax = 1。
 * 两个把手允许重叠（单档筛选），拖过头（下限 > 上限）自动交换而不是变空集。
 */
function sizeBandRaw() {
  const a = parseInt(($('#size_min') || {}).value, 10);
  const b = parseInt(($('#size_max') || {}).value, 10);
  return [isNaN(a) ? 0 : a, isNaN(b) ? SIZE_N - 1 : b];
}
function sizeBand() {
  const [a, b] = sizeBandRaw();
  return a > b ? [b, a] : [a, b];
}
function sizeBandText(smin, smax) {
  smin = Math.max(0, Math.min(SIZE_N - 1, smin));
  smax = Math.max(0, Math.min(SIZE_N - 1, smax));
  if (smin <= 0 && smax >= SIZE_N - 1) return '全部';
  if (smin <= 0) return '最小' + SIZE_PCT[smax];
  if (smin === smax) return SIZE_PCT[smin - 1] + '~' + SIZE_PCT[smax];
  if (smax >= SIZE_N - 1) return '剔除最小' + SIZE_PCT[smin - 1];
  return SIZE_PCT[smin - 1] + '~' + SIZE_PCT[smax];
}
/* 从任意「参数字典」里取出规范化后的市值区间（老方案只有 size_max 也适用） */
function sizeBandOf(p) {
  const one = (v, d) => {
    const n = parseInt(v, 10);
    return isNaN(n) ? d : Math.max(0, Math.min(SIZE_N - 1, n));
  };
  const a = one((p || {}).size_min, 0), b = one((p || {}).size_max, SIZE_N - 1);
  return a > b ? [b, a] : [a, b];
}
function syncSize() {
  const [lo, hi] = sizeBand();
  const v = $('#size_v');
  if (v) v.textContent = sizeBandText(lo, hi);
  const fill = $('#size_fill');
  if (fill) {
    // 轨道上把手的位置 = i/(N-1)（range 的原生行为），填充条按同一把尺子画，
    // 但不能画成「两个把手之间」—— 两个把手重叠（smin = smax = 只取一档）时
    // 宽度会变成 0，看上去像「什么都没选」。所以右边界固定用**该档的右沿**
    // (hi+1)/(N-1)，保证任何区间都至少有一档的宽度。
    const span = SIZE_N - 1;
    const left = lo / span;
    const right = Math.min(1, (hi + 1) / span);
    fill.style.left = (left * 100) + '%';
    fill.style.right = ((1 - right) * 100) + '%';
  }
  $$('#size_quick button').forEach(b => b.classList.toggle(
    'on', Number(b.dataset.smin) === lo && Number(b.dataset.smax) === hi));
}
function applySize(smin, smax) {
  const [lo, hi] = [smin, smax].map(v => {
    const n = parseInt(v, 10);
    return isNaN(n) ? null : Math.max(0, Math.min(SIZE_N - 1, n));
  });
  const a = lo === null ? 0 : lo, b = hi === null ? SIZE_N - 1 : hi;
  $('#size_min').value = String(Math.min(a, b));
  $('#size_max').value = String(Math.max(a, b));
  syncSize();
}
function collect() {
  const c = [];
  $$('#confirms input:checked').forEach(i => c.push(i.value));
  const m60 = segVal('#seg-ma60'), m120 = segVal('#seg-ma120'), r20 = segVal('#seg-ret20');
  const rs = segVal('#seg-rs'), ihv = segVal('#seg-ihv'), rv = segVal('#seg-rv');
  // 财务：界面用「亿元 / %」，后端用「元 / 小数」
  const fnum = id => { const v = $('#' + id).value.trim(); return v === '' ? null : parseFloat(v); };
  const toYuan = v => (v === null || !isFinite(v)) ? null : v * 1e8;
  const toFrac = v => (v === null || !isFinite(v)) ? null : v / 100;
  return {
    px_ma60_min: nz(m60 && m60.lo), px_ma60_max: nz(m60 && m60.hi),
    px_ma120_min: nz(m120 && m120.lo), px_ma120_max: nz(m120 && m120.hi),
    ret20_min: nz(r20 && r20.lo), ret20_max: nz(r20 && r20.hi),
    deep_any: $('#deep_any').checked,
    // 市值区间：两个序号都发（后端 norm_size_band 会再兜底一次交换/夹取）
    size_min: sizeBand()[0],
    size_max: sizeBand()[1],
    mkt_state: segv('#seg-mkt') || 'any',
    mkt_hv: segv('#seg-hv') || 'any',
    // 市场指数口径 + 均线窗口（牛熊判定用哪批股票、比多少日均线）
    mkt_index: $('#mkt_index').value || 'all',
    mkt_ma: parseInt($('#mkt_ma').value, 10) || 60,
    rs_sz_min: nz(rs && rs.lo), rs_sz_max: nz(rs && rs.hi),
    hv_min: nz(ihv && ihv.lo), hv_max: nz(ihv && ihv.hi),
    rvol_min: nz(rv && rv.lo), rvol_max: nz(rv && rv.hi),
    confirm: c,
    hold: parseInt($('#hold').value, 10),
    // ---- 容量约束（实盘可执行性）：0 = 不限仓位
    max_pos: (segv('#seg-cap') === '1') ? Math.max(1, parseInt($('#cap_maxpos').value, 10) || 10) : 0,
    max_new: Math.max(1, parseInt($('#cap_maxnew').value, 10) || 3),
    pick: $('#cap_pick').value || 'deep',
    // 建仓节奏：null = 等权（每笔一样大）。
    // ⚠️ 「不限仓位」口径下必须发 null —— 否则用户切回不限仓位后，
    //    残留的只数序列会让后端仍然按容量约束算，与「口径」开关自相矛盾。
    cap_ladder: (segv('#seg-cap') === '1' && ladderNow())
      ? ($('#cap_ladder').value.trim() || null) : null,
    // ---- 财务（动态）
    rev_min: toYuan(fnum('fin_rev_min')), rev_max: toYuan(fnum('fin_rev_max')),
    rev_yoy_min: toFrac(fnum('fin_ry_min')), rev_yoy_max: toFrac(fnum('fin_ry_max')),
    np_min: toYuan(fnum('fin_np_min')), np_max: toYuan(fnum('fin_np_max')),
    np_yoy_min: toFrac(fnum('fin_ny_min')), np_yoy_max: toFrac(fnum('fin_ny_max')),
    fin_period: $('#fin_period').value || 'latest',
    // ---- 退市风险过滤（全部基于买入日已公开的财报 / 行情）
    delist: {
      financial: !!($('#dl_fin') && $('#dl_fin').checked),
      loss2y: !!($('#dl_loss2y') && $('#dl_loss2y').checked),
      penny: !!($('#dl_penny') && $('#dl_penny').checked),
    },
    delist_rev_floor: (() => { const e = $('#dl_rev_floor'); const v = e ? parseFloat(e.value) : NaN; return isFinite(v) ? v * 1e8 : null; })(),
    delist_main_2024: !!($('#dl_main2024') && $('#dl_main2024').checked),
    delist_penny_days: (() => { const e = $('#dl_penny_days'); const v = e ? parseInt(e.value, 10) : NaN; return isFinite(v) ? v : null; })()
  };
}
/* 财务条件生效状态提示 */
function updateFinBadge() {
  const el = $('#fin_stat'); if (!el) return;
  const q = [];
  const fnum = id => { const v = $('#' + id).value.trim(); return v === '' ? null : parseFloat(v); };
  const box = (lo, hi, nm, unit) => {
    if (lo === null && hi === null) return;
    q.push(`${nm} ${lo === null ? '' : '≥' + lo}${(lo !== null && hi !== null) ? ' 且 ' : ''}${hi === null ? '' : (lo === null ? '≤' : '') + hi}${unit}`);
  };
  box(fnum('fin_rev_min'), fnum('fin_rev_max'), '营收', '亿');
  box(fnum('fin_ry_min'), fnum('fin_ry_max'), '营收同比', '%');
  box(fnum('fin_np_min'), fnum('fin_np_max'), '归母', '亿');
  box(fnum('fin_ny_min'), fnum('fin_ny_max'), '归母同比', '%');
  const fp = $('#fin_period').value;
  if (fp && fp !== 'latest') q.push('仅' + ({ q1: '一季报', q2: '半年报', q3: '三季报', q4: '年报' }[fp] || fp));
  if (q.length === 0) {
    el.innerHTML = '未启用财务筛选';
    el.style.color = '';
  } else {
    el.innerHTML = `已启用 ${q.length} 项 · ${q.join(' + ')}`;
    el.style.color = '#0d7a3a';
  }
}
/* 退市风险过滤生效状态提示 */
function updateDelistBadge() {
  const bg = $('#dl_badge'), st = $('#dl_stat');
  if (!bg) return;
  const on = {
    '财务类退市风险': !!($('#dl_fin') && $('#dl_fin').checked),
    '连续两年亏损': !!($('#dl_loss2y') && $('#dl_loss2y').checked),
    '面值退市': !!($('#dl_penny') && $('#dl_penny').checked),
  };
  const act = Object.keys(on).filter(k => on[k]);
  if (act.length === 0) {
    bg.textContent = '已关闭'; bg.style.color = ''; bg.style.background = '';
    if (st) { st.textContent = '未启用（默认口径 = 研究报告原始口径）'; st.style.color = ''; }
    return;
  }
  bg.textContent = `已启用 ${act.length} 项`; bg.style.color = '#0d7a3a';
  const parts = [act.join(' + ')];
  if (on['财务类退市风险']) {
    const fl = parseFloat(($('#dl_rev_floor') || {}).value);
    parts.push(`营收门槛 ${isFinite(fl) ? fl : 1} 亿${$('#dl_main2024') && $('#dl_main2024').checked ? '（主板 3 亿）' : ''}`);
  }
  if (on['面值退市']) {
    const d = parseInt(($('#dl_penny_days') || {}).value, 10);
    parts.push(`近20日中 ≥${isFinite(d) ? d : 20} 日收盘 < 1 元`);
  }
  if (st) { st.textContent = '已剔除：' + parts.join('；'); st.style.color = '#0d7a3a'; }
}
/* 财务快捷按钮 */
function setFinQuick(kind) {
  const set = (id, v) => { $('#' + id).value = (v === null ? '' : v); };
  if (kind === 'clear') {
    ['fin_rev_min', 'fin_rev_max', 'fin_ry_min', 'fin_ry_max',
     'fin_np_min', 'fin_np_max', 'fin_ny_min', 'fin_ny_max'].forEach(i => set(i, null));
    $('#fin_period').value = 'latest';
  } else if (kind === 'rev_up') { set('fin_ry_min', 0); }
  else if (kind === 'rev20') { set('fin_ry_min', 20); }
  else if (kind === 'np_up') { set('fin_ny_min', 0); }
  else if (kind === 'big') { set('fin_rev_min', 10); }
  $$('.finquick button').forEach(b => b.classList.toggle('on', b.dataset.fin === kind && kind !== 'clear'));
  updateFinBadge();
}
function applyParams(p) {
  const setSeg = (id, lo, hi) => {
    $$(id + ' button').forEach(b => {
      b.classList.toggle('on', (b.dataset.lo || '') === String(lo ?? '') &&
        (b.dataset.hi || '') === String(hi ?? ''));
    });
  };
  setSeg('#seg-ma60', p.px_ma60_min, p.px_ma60_max);
  setSeg('#seg-ma120', p.px_ma120_min, p.px_ma120_max);
  setSeg('#seg-ret20', p.ret20_min, p.ret20_max);
  setSeg('#seg-rs', p.rs_sz_min, p.rs_sz_max);
  setSeg('#seg-ihv', p.hv_min, p.hv_max);
  setSeg('#seg-rv', p.rvol_min, p.rvol_max);
  $('#deep_any').checked = !!p.deep_any;
  // 市值区间：老方案只有 size_max（下限缺省 0）也要能正确落位
  applySize(p.size_min ?? 0, p.size_max ?? (SIZE_N - 1));
  $('#hold').value = p.hold ?? 20;
  const setV = (id, v) => $$(id + ' button').forEach(b =>
    b.classList.toggle('on', (b.dataset.v || '') === String(v ?? 'any')));
  setV('#seg-mkt', p.mkt_state ?? 'any');
  setV('#seg-hv', p.mkt_hv ?? 'any');
  // 市场指数口径 / 均线窗口：预设里没有这两项 → 回落到默认（全A等权 / MA60）
  const setSel = (id, v) => {
    const el = $(id);
    if (el && v != null && [...el.options].some(o => o.value === String(v))) {
      el.value = String(v);
    }
  };
  setSel('#mkt_index', p.mkt_index ?? DEFAULT_PARAMS.mkt_index);
  setSel('#mkt_ma', p.mkt_ma ?? DEFAULT_PARAMS.mkt_ma);
  syncMktCfgNote();
  const cf = new Set(p.confirm || []);
  $$('#confirms input').forEach(i => i.checked = cf.has(i.value));
  // ---- 财务字段：元 → 亿元，小数 → %
  const setN = (id, v, mul) => {
    const el = $('#' + id);
    if (v === null || v === undefined || !isFinite(v)) { el.value = ''; return; }
    const x = v * mul;
    // 整数不显示多余小数位（20 而非 20.0）
    el.value = Number.isInteger(x) ? String(x) : x.toFixed(Math.abs(x) < 1 ? 2 : 1);
  };
  setN('fin_rev_min', p.rev_min, 1e-8); setN('fin_rev_max', p.rev_max, 1e-8);
  setN('fin_np_min', p.np_min, 1e-8); setN('fin_np_max', p.np_max, 1e-8);
  setN('fin_ry_min', p.rev_yoy_min, 100); setN('fin_ry_max', p.rev_yoy_max, 100);
  setN('fin_ny_min', p.np_yoy_min, 100); setN('fin_ny_max', p.np_yoy_max, 100);
  $('#fin_period').value = p.fin_period || 'latest';
  // ---- 仓位约束（applyParams 用于预设/重置，预设默认不限仓位）
  LAD_AUTO_MP = null;      // 先清掉「阶梯自动接管的同时持仓/每日买入」，避免还原到旧值
  LAD_AUTO_MN = null;
  const mp = parseInt(p.max_pos ?? 0, 10) || 0;
  $$('#seg-cap button').forEach(b => b.classList.toggle('on',
    b.dataset.v === (mp > 0 ? '1' : '0')));
  if (p.max_pos) $('#cap_maxpos').value = mp;
  if (p.max_new) $('#cap_maxnew').value = p.max_new;
  if (p.pick && $('#cap_pick').querySelector(`option[value="${p.pick}"]`)) {
    $('#cap_pick').value = p.pick;
  }
  $$('.capquick button').forEach(b =>
    b.classList.toggle('on', b.dataset.cap === `${mp},${p.max_new ?? 3}`));
  // ---- 建仓节奏（预设可能带 cap_ladder；默认 null = 等权）
  const rsel = $('#cap_rhythm');
  if (rsel) {
    const cl = p.cap_ladder;
    const txt = Array.isArray(cl) ? cl.join(',') : (cl == null ? '' : String(cl));
    $('#cap_ladder').value = txt || '1,2,2,2,3';
    rsel.value = !txt ? ''
      : ([...rsel.options].some(o => o.value === txt) ? txt : '__custom__');
    syncLadder();
  }
  $$('.finquick button').forEach(b => b.classList.remove('on'));
  // ---- 退市风险过滤
  const dl = p.delist || {};
  const setChk = (id, v) => { const e = $('#' + id); if (e) e.checked = !!v; };
  setChk('dl_fin', dl.financial); setChk('dl_loss2y', dl.loss2y);
  setChk('dl_penny', dl.penny);
  setChk('dl_main2024', p.delist_main_2024);
  // ⚠️ 判空必须用 `!= null`（同时排除 null 与 undefined），不能用 isFinite：
  //    内置预设的这两个键是**显式 null**，而 `isFinite(null)` 竟然是 true
  //    （Number(null) === 0）→ 会把输入框的默认值 1 亿 / 10 日冲成 0 / 空。
  //    这是靠刷新 fixtures 才暴露出来的：旧快照里这两个键是**缺失**的，
  //    `isFinite(undefined)` 为 false，恰好绕过了这个坑。
  if (p.delist_rev_floor != null && isFinite(p.delist_rev_floor)) {
    $('#dl_rev_floor').value = p.delist_rev_floor / 1e8;
  }
  if (p.delist_penny_days != null && isFinite(p.delist_penny_days)) {
    $('#dl_penny_days').value = p.delist_penny_days;
  }
  syncLabels();
  syncCap();
  updateModeBadge();
  updateDelistBadge();
  updateFinBadge();
}
function syncLabels() {
  syncSize();
  $('#hold_v').textContent = $('#hold').value + '日';
}

/* ---------------- 分段按钮绑定 ---------------- */
function bindSeg(id, multi) {
  $$(id + ' button').forEach(b => b.addEventListener('click', () => {
    if (multi) { b.classList.toggle('on'); }
    else { $$(id + ' button').forEach(x => x.classList.remove('on')); b.classList.add('on'); }
    updateModeBadge();
  }));
}
/* 把一份参数（不依赖页面状态）摘要成「N 个条件 · 距MA60 D1 + 市值≤最小30% …」。
 * 顶部徽标与「我的方案」的描述行共用它 —— 保存时看到的摘要，
 * 之后从下拉里选回来必须是同一句话，否则用户没法确认自己存的是哪套。 */
function paramSummary(p) {
  let n = 0; const L = [];
  const rg = (a, b, nm) => {
    if (a === null && b === null) return;
    const x = a === null ? 1 : a, y = b === null ? 10 : b;
    L.push(x === y ? `${nm} D${x}` : `${nm} D${x}~D${y}`);
  };
  const nn = v => (v === undefined ? null : v);
  const sz = sizeBandOf(p);
  if (nn(p.px_ma60_min) !== null || nn(p.px_ma60_max) !== null) { n++; rg(p.px_ma60_min, p.px_ma60_max, '距MA60'); }
  if (nn(p.px_ma120_min) !== null || nn(p.px_ma120_max) !== null) { n++; rg(p.px_ma120_min, p.px_ma120_max, '距MA120'); }
  if (nn(p.ret20_min) !== null || nn(p.ret20_max) !== null) { n++; rg(p.ret20_min, p.ret20_max, 'ret20'); }
  if (p.deep_any) { n++; L.push('深度超跌'); }
  if (sz[0] > 0 || sz[1] < SIZE_N - 1) { n++; L.push('市值' + sizeBandText(sz[0], sz[1])); }
  // ⚠️ 标签必须跟随所选口径/窗口 —— 写死「市场<MA60」在用户切到 MA120 或小市值口径
  //    之后就会说谎。格式与后端 engine.mkt_label() 保持一致（`MA60·小50% 等权`）。
  if (p.mkt_state && p.mkt_state !== 'any') {
    n++;
    const mw = p.mkt_ma || 60;
    const mi = p.mkt_index || 'all';
    L.push(`${p.mkt_state === 'bear' ? '市场<' : '市场>'}MA${mw}`
      + (mi === 'all' ? '' : '·' + (MKT_NAME[mi] || mi)));
  }
  if (p.mkt_hv && p.mkt_hv !== 'any') { n++; L.push(p.mkt_hv === 'high' ? '市场高波动' : '市场低波动'); }
  if ((p.confirm || []).length) { n += p.confirm.length; L.push('确认×' + p.confirm.length); }
  // 财务（动态）
  const fq = [];
  if (nn(p.rev_min) !== null || nn(p.rev_max) !== null) fq.push('营收');
  if (nn(p.rev_yoy_min) !== null || nn(p.rev_yoy_max) !== null) fq.push('营收同比');
  if (nn(p.np_min) !== null || nn(p.np_max) !== null) fq.push('归母');
  if (nn(p.np_yoy_min) !== null || nn(p.np_yoy_max) !== null) fq.push('归母同比');
  if (fq.length) { n += fq.length; L.push('财务:' + fq.join('/')); }
  // 仓位约束（不是选股条件，单独提示）
  if ((p.max_pos || 0) > 0) { L.push(`仓位≤${p.max_pos}只/日${p.max_new}只`); }
  if (p.cap_ladder) {
    const cl = Array.isArray(p.cap_ladder) ? p.cap_ladder : String(p.cap_ladder).split(',');
    const s = cl.map(Number).filter(x => x > 0).reduce((a, b) => a + b, 0);
    if (s) L.push(`阶梯建仓满仓${s}只`);
  }
  return { n, L,
    text: n ? `${n} 个条件 · ` + L.slice(0, 4).join(' + ') + (L.length > 4 ? ' …' : '')
            : '无过滤 · 全市场基准' };
}
function updateModeBadge() {
  const s = paramSummary(collect());
  const b = $('#bmode');
  b.textContent = s.text;
  b.className = 'badge ' + (s.n >= 5 ? 'warn' : 'ok');
}

/* ---------------- 图形助手 ---------------- */
function mkChart(id) {
  const el = document.getElementById(id);
  if (!el) return null;
  if (charts[id]) charts[id].dispose();
  const c = echarts.init(el, null, { renderer: 'canvas' });
  charts[id] = c;
  return c;
}
const AX = {
  axisLine: { lineStyle: { color: '#d8dbe0' } },
  axisLabel: { color: '#8d94a3', fontSize: 10.5 },
  splitLine: { lineStyle: { color: '#f0f1f4' } }
};
const TIP = {
  backgroundColor: 'rgba(28,30,33,.92)', borderWidth: 0,
  textStyle: { color: '#fff', fontSize: 12 }, padding: [7, 11]
};
window.addEventListener('resize', () => Object.values(charts).forEach(c => c.resize()));

/* ---------------- Tabs ---------------- */
$$('#tabs button').forEach(b => b.addEventListener('click', () => {
  $$('#tabs button').forEach(x => x.classList.remove('on'));
  b.classList.add('on');
  $$('.pane').forEach(p => p.classList.remove('on'));
  $('#p-' + b.dataset.t).classList.add('on');
  // 交易明细：首次进入或参数已变时按需拉取
  if (b.dataset.t === 'trades' && !TR) runTrades();
  // 持仓盈亏：每次进入都重算（行情可能已更新），并退出编辑态
  if (b.dataset.t === 'hold') { HEDIT = null; runHoldings(); }
  setTimeout(() => Object.values(charts).forEach(c => c.resize()), 40);
}));

/* ---------------- 预设方案（内置 + 我的方案） ----------------
 * 「我的方案」存在服务端 data/user_presets.json（不是 localStorage）：
 * 方案应该跟着数据目录走 —— 换浏览器不丢，脚本也能直接读到。
 * 内置方案（报告已验证的 K 系列）只读，不允许删除。
 */
function presetById(id) {
  return PRESETS.find(x => x.id === id) || USER_PRESETS.find(x => x.id === id) || null;
}
function isUserPreset(id) { return USER_PRESETS.some(x => x.id === id); }
function curPreset() { return presetById($('#preset').value); }

function renderPresets(want) {
  const sel = $('#preset');
  const keep = want !== undefined ? want : sel.value;
  // 默认方案打标记：在 option 文字后加「 · 默认」，让用户一眼看到当前设置
  const mark = id => (id === DEFAULT_PRESET ? ' · 默认' : '');
  let html = '<optgroup label="内置方案（报告已验证）">' +
    PRESETS.map(p => `<option value="${esc(p.id)}">${esc(p.name)}${mark(p.id)}</option>`).join('') +
    '</optgroup>';
  if (USER_PRESETS.length) {
    html += '<optgroup label="我的方案（本地保存）">' +
      USER_PRESETS.map(p => `<option value="${esc(p.id)}">${esc(p.name)}${mark(p.id)}</option>`).join('') +
      '</optgroup>';
  }
  sel.innerHTML = html;
  const ids = [...sel.options].map(o => o.value);
  sel.value = ids.includes(keep) ? keep : (PRESETS[0] ? PRESETS[0].id : '');
  syncPresetUI();
}
/* 下拉旁的文字区：内置方案显示报告结论，我的方案显示保存时间 + 参数摘要 */
function syncPresetUI() {
  const p = curPreset();
  const mine = !!(p && isUserPreset(p.id));
  if (p) {
    $('#presetdesc').textContent = mine
      ? `我的方案${p.saved_at ? ' · 保存于 ' + p.saved_at : ''} · ` + paramSummary(p.params).text
        + (p.pool ? ' · ' + poolSummary(p.pool) : '')
      : (p.desc || '');
  }
  // 选中「我的方案」时把原名字回填：此时点保存 = 覆盖同名方案
  $('#preset_name').value = mine ? p.name : '';
  $('#preset_name').placeholder = mine ? '改个新名字 = 另存为一份' : '给当前这套参数起个名…';
  $('#preset_del').disabled = !mine;
  // 「设为默认」按钮：当前这套已是默认 → 禁用并改文案，避免重复点
  const isDef = !!(p && DEFAULT_PRESET === p.id);
  const bd = $('#preset_def');
  bd.disabled = isDef || !p;
  bd.textContent = isDef ? '已是默认方案' : '设为默认';
  bd.title = isDef
    ? '当前方案已设为界面默认'
    : '下次打开界面时默认选中这套方案（可选内置或我的方案）';
  // 「取消默认」只在确实有默认设置时可点
  const bc = $('#preset_def_clear');
  bc.disabled = !DEFAULT_PRESET;
  bc.title = DEFAULT_PRESET
    ? '取消默认设置，恢复成内置第一套'
    : '当前没有设置默认方案';
}
function setPresetStat(msg, kind) {
  const el = $('#preset_stat');
  if (!el) return;
  el.textContent = msg || '';
  el.style.color = kind === 'bad' ? '#c0392b' : (msg ? '#0d7a3a' : '');
}
/* 保存当前页面上的参数（不是「当前选中预设的参数」—— 用户可能已经改过几项）*/
async function savePreset() {
  const name = ($('#preset_name').value || '').trim();
  if (!name) {
    setPresetStat('先给这套参数起个名字', 'bad');
    $('#preset_name').focus();
    return;
  }
  const params = collect();
  const pool = currentPool();          // ⚠️ 股票池必须单独发，不在 params 白名单里
  setPresetStat('保存中…');
  try {
    const r = await api('/api/presets/save', { name, params, pool });
    if (!r || r.ok === false) { setPresetStat((r && r.msg) || '保存失败', 'bad'); return; }
    USER_PRESETS = r.presets || USER_PRESETS;
    renderPresets(r.id);
    setPresetStat((r.msg || '已保存') + ' · ' + paramSummary(params).text
      + ' · ' + poolSummary(pool));
  } catch (e) {
    setPresetStat('保存失败：' + (e && e.message ? e.message : e), 'bad');
  }
}
async function deletePreset() {
  const p = curPreset();
  if (!p || !isUserPreset(p.id)) { setPresetStat('内置方案不可删除', 'bad'); return; }
  if (!confirm(`删除方案「${p.name}」？此操作不可撤销。`)) return;
  try {
    const r = await api('/api/presets/delete', { id: p.id });
    if (!r || r.ok === false) { setPresetStat((r && r.msg) || '删除失败', 'bad'); return; }
    USER_PRESETS = r.presets || [];
    renderPresets(PRESETS[0] ? PRESETS[0].id : '');
    setPresetStat(`已删除「${p.name}」`);
  } catch (e) {
    setPresetStat('删除失败：' + (e && e.message ? e.message : e), 'bad');
  }
}

/* 设置 / 取消「界面默认预设」。
   选中哪套就设哪套 —— 内置方案也可以设为默认（它只是「默认选中」，
   不代表被修改）。设置存在服务端 data/default_preset.txt，换浏览器也在。 */
async function setDefaultPreset(pid) {
  const p = presetById(pid);
  if (!p) { setPresetStat('先选一套方案', 'bad'); return; }
  const isCur = DEFAULT_PRESET === pid;
  if (isCur) { setPresetStat(`「${p.name}」已经是默认方案`, 'bad'); return; }
  try {
    const r = await api('/api/presets/set_default', { id: pid });
    if (!r || r.ok === false) { setPresetStat((r && r.msg) || '设置失败', 'bad'); return; }
    DEFAULT_PRESET = r.default_preset || '';
    renderPresets(pid);              // 重绘以刷新「 · 默认」标记
    syncPresetUI();
    setPresetStat(r.msg || `已设为默认：${p.name}`);
  } catch (e) {
    setPresetStat('设置失败：' + (e && e.message ? e.message : e), 'bad');
  }
}
async function clearDefaultPreset() {
  if (!DEFAULT_PRESET) { setPresetStat('当前没有设置默认方案', 'bad'); return; }
  try {
    const r = await api('/api/presets/set_default', { id: '' });
    if (!r || r.ok === false) { setPresetStat((r && r.msg) || '取消失败', 'bad'); return; }
    DEFAULT_PRESET = '';
    renderPresets();
    setPresetStat(r.msg || '已取消默认设置');
  } catch (e) {
    setPresetStat('取消失败：' + (e && e.message ? e.message : e), 'bad');
  }
}

/* ---------------- 数据更新（顶栏按钮） ----------------
   为什么要轮询状态而不是等这一个请求返回：
   更新会**先停服再重建**（重建面板要独占 ~7GB），服务一停，请求就断了 ——
   HTTP 请求根本不可能撑满 18 分钟。所以后端立刻返回「已开始」，
   真正的进度写进 logs/update_state.json，前端另起轮询去读。
   服务停着的那段时间轮询必然失败，那是**预期行为**，不当错误处理。 */
let UPD_TIMER = null;

function updBtn(){ return $('#btn-upd'); }

function setUpdBtn(text, cls, disabled){
  const b = updBtn(); if (!b) return;
  b.textContent = text;
  b.className = 'updbtn' + (cls ? ' ' + cls : '');
  b.disabled = !!disabled;
}

function stopUpdPoll(){
  if (UPD_TIMER){ clearInterval(UPD_TIMER); UPD_TIMER = null; }
}

function startUpdPoll(){
  stopUpdPoll();
  UPD_TIMER = setInterval(pollUpd, 5000);
  pollUpd();
}

async function pollUpd(){
  let st;
  try {
    st = await api('/api/update/status', null, true);
  } catch (e) {
    // 服务正在重启（更新中途"停服"这一步）—— 预期内，继续等
    setUpdBtn('更新中…（服务重启中）', 'busy', true);
    $('#netinfo').innerHTML = '<span style="color:#b8860b">● 正在更新数据</span> · 服务重启中…';
    return;
  }
  const s = st && st.state;
  if (s === 'running' || s === 'starting'){
    const secs = st.started_at ? Math.round(Date.now()/1000 - st.started_at) : 0;
    const mm = Math.floor(secs/60), ss = secs % 60;
    setUpdBtn(`更新中 ${mm}:${String(ss).padStart(2,'0')}`, 'busy', true);
    $('#netinfo').innerHTML = '<span style="color:#b8860b">● 正在更新数据</span> · ' +
      '增量取数 → 重建面板 → 自动重启服务（约 18 分钟，期间服务不可用）';
    return;
  }
  // done / failed / idle
  stopUpdPoll();
  // 陈旧结论（超过 30 分钟前的 done/failed）不再展示，避免每次打开页面都看到旧提示
  const age = (st && st.finished_at) ? Date.now()/1000 - st.finished_at : 1e9;
  if (age > 1800) { setUpdBtn('更新数据', '', false); return; }
  if (s === 'done'){
    setUpdBtn('更新完成，重载中…', 'busy', true);
    $('#netinfo').innerHTML = '<span style="color:#17764f">● 数据已更新</span> · 正在重新加载…';
    setTimeout(() => location.reload(), 2500);
  } else if (s === 'failed'){
    setUpdBtn('更新失败，点击重试', '', false);
    $('#netinfo').innerHTML = '<span style="color:#a83232">● 更新失败</span> · ' +
      ((st && st.msg) || '') + '　详见 logs/update_run.log';
  } else {
    setUpdBtn('更新数据', '', false);
  }
}

async function onUpdateClick(){
  const b = updBtn();
  if (!b || b.disabled) return;
  if (!confirm('将执行完整数据更新：\n\n  增量取数 → 重建面板 → 自动重启服务\n\n'
             + '约需 18 分钟，期间本页面会不可用。确定开始吗？')) return;
  setUpdBtn('启动中…', 'busy', true);
  try {
    const r = await api('/api/update/start', {});
    if (r && r.ok === false){
      setUpdBtn('更新数据', '', false);
      $('#netinfo').innerHTML = '<span style="color:#b8860b">● 未开始更新</span> · ' +
        ((r && r.msg) || '已有更新在进行中');
      return;
    }
    $('#netinfo').innerHTML = '<span style="color:#b8860b">● 正在更新数据</span> · 已启动…';
    startUpdPoll();
  } catch (e) {
    setUpdBtn('更新失败，点击重试', '', false);
    $('#netinfo').innerHTML = '<span style="color:#a83232">● 启动更新失败</span> · ' +
      (e && e.message ? e.message : e);
  }
}

/* ---------------- 初始化 ---------------- */
async function init() {
  try {
    const [m, d] = await Promise.all([api('/api/meta', null, true), api('/api/defaults', null, true)]);
    META = m; DEFAULT_PARAMS = d.defaults; PRESETS = d.presets;
    $('#netinfo').innerHTML = `<span style="color:#17764f">● 已连接</span> · ` +
      `${NUM(m.n_stocks)} 只 / ${NUM(m.n_days)} 交易日 / ${m.date_start} ~ ${m.date_end}`;
    $('#bdate').textContent = '数据截止 ' + m.date_end;
    $('#bdate').className = 'badge ok';
    LAST = m.last_date;
    // 市值分位的百分比表由后端给（避免两端各写一份对不上）
    if (d.size_bands && Array.isArray(d.size_bands.pct)) SIZE_PCT = d.size_bands.pct;
    // 预设：内置 + 我的方案
    USER_PRESETS = d.user_presets || [];
    // 「界面默认预设」：优先用它，但它可能指向**已不存在**的方案
    // （方案被删 / 文件被手改）→ 必须校验存在性再回退，否则下拉会空白。
    DEFAULT_PRESET = d.default_preset || '';
    const startId = (DEFAULT_PRESET && presetById(DEFAULT_PRESET))
      ? DEFAULT_PRESET : (PRESETS[0] ? PRESETS[0].id : '');
    if (DEFAULT_PRESET && !presetById(DEFAULT_PRESET)) {
      // 静默修正：清掉失效的设置，免得每次打开都白跑一次校验
      DEFAULT_PRESET = '';
      api('/api/presets/set_default', { id: '' }).catch(() => {});
    }
    renderPresets(startId);
    // ⚠️ 必须在 applyParams 之前填充，否则 cap_pick 还没有 option，选不中
    fillPickRules(d.pick_rules);
    fillLadderPresets(d.ladder_presets);
    fillRhythmAxis(d.rhythm_cands);
    // 同上：市场口径 / 均线窗口的 option 必须先有，applyParams 才选得中
    fillMktOptions(m);
    const startP = presetById(startId) || PRESETS[0];
    applyParams(startP.params);
    CFG = { ...DEFAULT_PARAMS, ...startP.params };
    // 若上一次的更新还在跑（页面被刷新 / 重开），接着显示进度 —— 不要让人以为丢了
    try {
      const ust = await api('/api/update/status', null, true);
      if (ust && (ust.state === 'running' || ust.state === 'starting')) startUpdPoll();
      else if (ust && ust.state === 'done') setUpdBtn('更新数据', '', false);
    } catch (e) { /* 状态读不到不影响主流程 */ }
    // 行业（两级：门类 → 细分行业）
    renderIndustryTree(m);
    // 板块数量徽章（来自 meta.board_counts，仅供提示；未连接时留空）
    const bc = m.board_counts || {};
    ['MAIN', 'CHINEXT', 'STAR', 'BJ'].forEach(k => {
      const el = $('#sbc-' + k);
      if (el && bc[k] != null) el.textContent = NUM(bc[k]);
    });
    syncBdCnt();
    // ⚠️ 股票池的**两段式**应用（顺序不能反）：
    //    ① 先无条件套用 defaultPool() —— 让「默认池」有唯一真相源（JS），
    //       HTML 里板块按钮的静态 class 只当首屏占位，不再参与逻辑。
    //       （踩过：把 defaultPool 改成只主板，页面仍勾着三板块 ——
    //        因为 HTML 里那几个 class="on" 是写死的，两处漂移了。）
    //    ② 再用方案自己的 pool 覆盖 —— 方案优先级更高（用户存的池子要保住）。
    //       方案没有 pool（内置 K 系列 / 早期方案）→ applyPool 返回 false，保持 ① 的结果。
    //    ⚠️ 必须在 renderIndustryTree 之后：applyPool 要勾 `#inds input.ibx`，
    //       而行业树这一刻才生成；更早调用 → 行业勾选全部丢失。
    applyPool(defaultPool());
    applyPool(startP.pool);
    $('#p-help').innerHTML = helpHTML();
    await runAll();
  } catch (e) {
    $('#netinfo').innerHTML = '<span style="color:#a83232">● 连接失败</span> ' + e.message;
  }
}

/* ---------------- 行业两级树（门类 → 细分行业） ----------------
   数据源只有同花顺 88 个细分行业，没有门类字段。门类是后端按
   app/industry_tree.json 做的**人工归并**（局限见该文件头部说明）。
   前端只做展示与交互：请求体里仍然只发**细分行业名**的扁平清单
   （pool.industries），所以后端过滤逻辑完全不用改。 */
const ITREE = { groups: [], absent: [], unlisted: [], unlisted_n: null };
const esc = s => String(s).replace(/[&<>"]/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

function _indGroupHTML(code, name, subs, nstock, title) {
  return `
    <div class="igt" data-code="${esc(code)}">
      <div class="igh" data-code="${esc(code)}"${title ? ` title="${esc(title)}"` : ''}>
        <input type="checkbox" class="igc" data-grp="${esc(code)}" title="全选本门类下的全部细分行业">
        <span class="igcode">${esc(code)}</span>
        <span class="ignm">${esc(name)}</span>
        <span class="igcnt">${subs.length} 行业${nstock != null ? ' · ' + NUM(nstock) + ' 只' : ''}</span>
        <span class="igarrow">▶</span>
      </div>
      <div class="igb" data-code="${esc(code)}">
        ${subs.map(s => `<label class="chk"><input type="checkbox" class="ibx" value="${esc(s)}"><span><span class="t">${esc(s)}</span></span></label>`).join('')}
      </div>
    </div>`;
}

function renderIndustryTree(m) {
  const box = $('#inds');
  if (!box) return;
  ITREE.groups = m.industry_tree || [];
  ITREE.absent = m.industry_absent || [];
  ITREE.unlisted = m.industry_unlisted || [];
  ITREE.unlisted_n = m.industry_unlisted_n;

  // 兜底：后端没给出树（配置缺失等）→ 退回原来的单层行业列表，功能不丢
  if (!ITREE.groups.length) {
    box.innerHTML = (m.industries || []).map(n =>
      `<label class="chk"><input type="checkbox" class="ibx" value="${esc(n)}"><span><span class="t">${esc(n)}</span></span></label>`).join('');
    syncGrpState(); syncIndCnt();
    return;
  }

  let html = ITREE.groups.map(g => _indGroupHTML(g.code, g.name, g.subs, g.n_stock)).join('');
  // 兜底分组：面板里有、配置却没归类的细分行业单独成组 —— 绝不静默丢弃。
  // 实测必然包含「未知」（个股缺行业映射时的兜底值），这行能让缺失量级可见。
  if (ITREE.unlisted.length) {
    html += _indGroupHTML('—', '未归类', ITREE.unlisted, ITREE.unlisted_n,
      '面板里有、但行业归类配置未覆盖的细分行业。含「未知」= 该股缺行业映射（数据源未收录）。');
  }
  // 数据源没有细分行业的门类：如实列出并说明原因（悬停看详情）
  html += ITREE.absent.map(a => `
    <div class="iga" title="${esc(a.why || '')}">
      <span class="igcode">${esc(a.code)}</span>
      <span class="ignm">${esc(a.name)}</span>
      <span class="igmark">本数据源无细分行业</span>
    </div>`).join('');

  box.innerHTML = html;
  syncGrpState();
  syncIndCnt();
}

/* 门类复选框的三态：全选 / 半选(indeterminate) / 未选 */
function syncGrpState() {
  $$('#inds .igt').forEach(g => {
    const head = g.querySelector('.igh'), gbox = g.querySelector('.igc');
    const subs = [...g.querySelectorAll('.ibx')];
    const on = subs.filter(i => i.checked).length;
    if (gbox) {
      gbox.checked = subs.length > 0 && on === subs.length;
      gbox.indeterminate = on > 0 && on < subs.length;
    }
    if (head) head.classList.toggle('some', on > 0);
  });
}

/* 行业搜索：过滤细分行业；命中则自动展开所属门类，清空后恢复原展开状态 */
function filterInds(q) {
  q = String(q || '').trim();
  const wrap = $('#indsearch');
  if (wrap) wrap.classList.toggle('on', !!q);
  const needle = q.toLowerCase();
  $$('#inds .igt').forEach(g => {
    if (q && g.dataset.was === undefined) {      // 进入搜索前先记住展开状态
      g.dataset.was = g.classList.contains('indopen') ? '1' : '0';
    }
    let hit = 0;
    g.querySelectorAll('.ibx').forEach(i => {
      const lab = i.closest('label');
      const m = !q || lab.textContent.toLowerCase().indexOf(needle) >= 0;
      lab.hidden = !m;
      if (m) hit++;
    });
    g.hidden = hit === 0;
    if (q) {
      g.classList.add('indopen');
    } else {
      g.classList.toggle('indopen', g.dataset.was === '1');
      delete g.dataset.was;
    }
  });
}

/* ---------------- 主流程 ---------------- */
function currentPool() {
  const p = { ...POOL };
  p.mode = segv('#seg-pool') || 'all';
  p.codes_text = $('#pool_codes').value;
  p.industries = $$('#inds input.ibx:checked').map(i => i.value);
  p.boards = $$('#seg-board button.on').map(b => b.dataset.v);
  p.exchanges = $$('#seg-ex button.on').map(b => b.dataset.v);
  return p;
}
/* ---------------- 股票池：默认值 / 摘要 / 回填 ----------------
   ⚠️ 股票池不在 collect() 的 params 里（走 currentPool() 的 pool 字段）。
      保存方案时必须**两个都发**：只发 params 的话，板块/行业/自定义代码/
      交易所这一整组都会丢 —— 也就是「保存后切走再切回，板块筛选没了」。 */
const EXN = { SH: '沪市', SZ: '深市', BJ: '北交所' };
/* 默认股票池 —— **只主板**。
   ⚠️ 为什么改成只主板（2026-10-12 用户要求「回测只用主板的数据」）：
      · 主板是这套「超跌反转」最稳的战场（成交活跃、退市规则清晰、无涨跌幅 20% 的制度差异）；
      · 创业板/科创板 20% 涨跌幅 + 上市未满期票，会让回撤显著放大；
      · 北交所流动性与制度差异更大，本来就不选。
   ⚠️ 这只是**默认值**：用户随时可以在 ③ 板块 里改（或存进方案随方案走）。
      改这里会同时影响：回测 / 选股 / 寻优 / 单股透视 / 导出 ——
      因为它们都走 `currentPool()`，也正是「一处改、处处生效」的原因。
   ⚠️ 与「我的方案」的 pool 优先级：方案里存了 pool 就以方案为准（见 applyPool）。 */
function defaultPool() {
  return {
    mode: 'all', codes_text: '', industries: [],
    boards: ['MAIN'],                           // 只主板
    exchanges: ['SH', 'SZ'],
  };
}
/* 股票池的有效条件文案（只列真正收窄了范围的项） */
function poolConds(p) {
  p = p || {};
  const L = [];
  const bd = (p.boards || []).map(String);
  if (bd.length && bd.length < 4) L.push('板块:' + bd.map(BD).join('/'));
  const ex = (p.exchanges || []).map(String);
  if (ex.length && ex.length < 3) L.push('交易所:' + ex.map(x => EXN[x] || x).join('/'));
  const inds = p.industries || [];
  if (inds.length) L.push(`行业×${inds.length}`);
  if (p.mode === 'custom') {
    const t = String(p.codes_text || '').replace(/[,，]/g, ' ').trim();
    const n = t ? t.split(/\s+/).filter(Boolean).length : 0;
    L.push(n ? `自选池 ${n} 只` : '自选池（空）');
  }
  return L;
}
function poolSummary(p) {
  const L = poolConds(p);
  return L.length ? '股票池 ' + L.join(' + ') : '股票池 不限';
}
/* 把方案里的股票池回填到页面。pool 为空（内置预设 / 早期保存的方案）时
   **什么都不做**，保持页面现有池子不变 —— 刻意的向后兼容。 */
function applyPool(q) {
  if (!q) return false;
  const p = Object.assign(defaultPool(), q);
  const codes = String(p.codes_text || '');
  $('#pool_codes').value = codes;
  const nc = codes.replace(/[,，]/g, ' ').split(/\s+/).filter(Boolean).length;
  $('#poolcnt').textContent = `已输入 ${nc} 个代码`;
  const mode = p.mode === 'custom' ? 'custom' : 'all';
  $$('#seg-pool button').forEach(b => b.classList.toggle('on', b.dataset.v === mode));
  $('#poolcustom').style.display = mode === 'custom' ? 'block' : 'none';
  // 行业树：先清搜索框，否则被过滤隐藏的复选框即使勾上也看不见
  if (IND_Q) { IND_Q.value = ''; filterInds(''); }
  const inds = new Set(p.industries || []);
  $$('#inds input.ibx').forEach(i => i.checked = inds.has(i.value));
  syncGrpState(); syncIndCnt();
  const bd = new Set((p.boards || []).map(x => String(x).toUpperCase()));
  $$('#seg-board button').forEach(b => b.classList.toggle('on', bd.has(b.dataset.v)));
  syncBdCnt();
  const ex = new Set((p.exchanges || []).map(x => String(x).toUpperCase()));
  $$('#seg-ex button').forEach(b => b.classList.toggle('on', ex.has(b.dataset.v)));
  POOL = Object.assign({}, p);
  return true;
}
async function runAll() { await runBacktest(); await runScan(); }

async function runScan(quiet) {
  const btn = $('#run');
  if (!quiet) btn.innerHTML = '<span class="spin"></span> 计算中';
  try {
    const [r, mk] = await Promise.all([
      api('/api/scan', { params: collect(), pool: currentPool(), limit: 120 }),
      loadMarket(),
    ]);
    SCAN = r;
    renderScan(r, mk);
    $('#c-scan').textContent = r.n ? `(${NUM(r.n)})` : '';
  } catch (e) {
    $('#p-scan').innerHTML = `<div class="errbox"><b>选股失败：</b>${e.message}</div>`;
  } finally { if (!quiet) btn.textContent = '运行回测'; }
}

/* 市场环境序列（自建净值 vs 均线）+ 真实指数对照。
   ⚠️ 现在**跟随「指数口径 / 均线窗口」两个参数** —— 换口径会换出另一条线，
      不能再像以前那样「整个会话缓存一次」。
      缓存按 `index|ma` 分键：同一会话里来回切换不会重复请求，换参数则必然重取。
      取不到就返回 null（可选数据，不该拖垮选股页）。 */
let MKT = null, MKT_KEY = '';
async function loadMarket(force) {
  const ix = ($('#mkt_index') && $('#mkt_index').value) || 'all';
  const ma = ($('#mkt_ma') && $('#mkt_ma').value) || '60';
  const key = `${ix}|${ma}`;
  if (MKT && MKT_KEY === key && !force) return MKT;
  try {
    MKT = await api(`/api/market?days=180&index=${encodeURIComponent(ix)}&ma=${encodeURIComponent(ma)}`,
                    null, true);
    MKT_KEY = key;
  } catch (e) { MKT = null; MKT_KEY = ''; }
  return MKT;
}

/* 牛熊徽标 —— ⚠️ `bull === null` 表示「均线未成形」，必须与 false（跌破）区分开。
   写成 `x.bull ? '牛' : '熊'` 会把 null 误显示成熊市。 */
const bullBadge = b => (b === null || b === undefined)
  ? '<span class="neu">—</span>'
  : (b ? '<span class="pos">牛市</span>' : '<span class="neg">熊市</span>');

function mktBlock(mk) {
  if (!mk || !mk.series || !mk.series.length) return '';
  const idx = mk.indices || [];
  const last = mk.series[mk.series.length - 1];
  const mw = mk.ma_win || 60;
  const ixName = mk.index_name || '全A等权';
  const rows = idx.map(x => `<tr>
      <td class="l">${esc(x.name)}</td>
      <td>${F(x.close, 2)}</td><td>${F(x.ma60, 2)}</td>
      <td class="${cls(x.dist)}">${F(x.dist, 2, '%')}</td>
      <td>${bullBadge(x.bull)}</td>
      <td class="l">${esc(x.last_date)}</td></tr>`).join('');
  const bears = idx.filter(x => x.bull === false).length;
  // 只在「引擎说牛市、但真实指数里有熊市」时才告警 —— 这才是会让人误判的那种分歧
  const split = (mk.bull_now === true && bears > 0);
  const warn = split ? `<div class="warnbox">⚠️ <b>口径分歧</b>：本策略的牛熊判定用的是
    <b>自建${esc(ixName)}净值</b>（日度再平衡、开盘→开盘），它与市值加权指数
    （上证/沪深300）<b>不是同一条线</b>，所以会出现
    「自建净值在 MA${mw} 上方」而<b>真实指数全在 MA60 下方</b>的情况。<br>
    <b>策略实际用的是自建净值</b>（上表第一行）；想按真实指数判断市场，请看下面几张指数。</div>` : '';
  // 同一口径下三个窗口的当前状态 —— 这是「换窗口会怎样」的**可验证依据**，
  // 不能只在左侧用一句文案警告（用户无从核对）。
  const wins = (mk.windows || []).map(w => `<tr>
      <td class="l">MA${w.ma}${String(w.ma) === String(mw) ? ' <span class="tag">当前</span>' : ''}</td>
      <td class="${cls(w.dist)}">${F(w.dist, 2, '%')}</td>
      <td>${bullBadge(w.bull)}</td></tr>`).join('');
  const winTbl = wins ? `<div class="tw" style="margin-top:10px"><table><thead><tr>
      <th style="text-align:left">同一口径 · 换均线窗口</th><th>距均线</th><th>状态</th>
    </tr></thead><tbody>${wins}</tbody></table></div>
    <div class="cap">⚠️ <b>换均线窗口与换指数口径效果几乎等价</b>（都等于把过滤变严）。
      实测 CAGR：全A 在 MA20/60/120 下为 25.93% / 29.81% / 34.86%；
      小50% 为 <b>16.81%</b> / 35.19% / <b>43.64%</b> —— 跨度 26.8pp，
      而全A 只有 8.9pp。只看单一窗口的数字会高估小市值口径的稳健性。</div>` : '';
  return `<div class="card" style="background:#fafbfc">
    <h3 style="font-size:13px">市场环境 <span class="tag">牛熊判定依据</span></h3>
    <div class="desc">策略的「熊市（市场净值 &lt; MA${mw}）」过滤用的是
      <b>自建${esc(ixName)}净值</b>，不是交易所指数（指数数据本地只覆盖 2023 起，
      做不了 11 年回测）。口径与均线窗口可在左侧「③ 市场环境」里切换。
      两者在个别时段会明显分歧，故并列展示。</div>
    <div class="tw"><table><thead><tr>
      <th style="text-align:left">口径</th><th>最新值</th><th>MA${mw}</th>
      <th>距MA${mw}</th><th>状态</th><th style="text-align:left">数据日</th>
    </tr></thead><tbody>
      <tr><td class="l"><b>自建${esc(ixName)}净值</b> <span class="tag">策略用</span></td>
        <td>${F(last.nav, 4)}</td>
        <td>${F(last.ma, 4)}</td>
        <td class="${cls(mk.dist_now)}">${F(mk.dist_now, 2, '%')}</td>
        <td>${bullBadge(mk.bull_now)}</td>
        <td class="l">${esc(mk.last_date)}</td></tr>
      ${rows}
    </tbody></table></div>
    ${warn}
    ${winTbl}
    <div class="cap">自建净值的口径：当日该口径成分股「可交易开盘→开盘」收益的<b>等权平均</b>逐日复利，
      因此 <b>T 日的值反映的是 T 日开盘</b>（最后一天没有 T+1，当日收益恒为 0）。
      真实指数为<b>收盘价 vs 自身 60 日简单均线</b>。指数数据来自
      <code>data/raw/index_*.json</code>，缺失时本表只显示自建净值一行。</div></div>`;
}

async function runBacktest() {
  const b = $('#run');
  b.innerHTML = '<span class="spin"></span> 回测中';
  b.disabled = true;
  // 参数变了 → 交易明细需要重新拉取
  TR = null;
  try {
    const r = await api('/api/backtest', { params: collect(), pool: currentPool() });
    renderBacktest(r);
    // 若当前正停在「交易明细」页，同步刷新
    if ($('#tabs button.on') && $('#tabs button.on').dataset.t === 'trades') {
      runTrades();
    }
  } catch (e) {
    $('#p-bt').innerHTML = `<div class="errbox"><b>回测失败：</b>${e.message}</div>`;
  } finally { b.textContent = '运行回测'; b.disabled = false; }
}

/* ---------------- 渲染：今日选股 ---------------- */
function renderScan(r, mk) {
  // ⚠️ 文案必须跟随所选口径/窗口 —— 写死「MA60」会在用户切到 MA120 或小市值口径后说谎。
  const lb = r.mkt_label || 'MA60';
  const mkt = r.mkt_bull === null ? '' : (r.mkt_bull
    ? `<span style="color:#a8622c">牛市（净值 &gt; ${esc(lb)}）</span>`
    : `<span style="color:#17764f">熊市（净值 &lt; ${esc(lb)}）</span>`);
  let fb = '';
  if (r.fallback_days > 0) {
    fb = `<div class="warnbox"><b>已回溯到最近一个有信号的交易日：${r.asof}</b>
      （距最新交易日 ${r.latest_date} 回退 ${r.fallback_days} 个交易日）。<br>
      原因是当前条件含「熊市（市场净值 &lt; ${esc(lb)}）」，而最新交易日
      <b>${r.latest_date}</b> 市场处于
      ${r.mkt_bull_now ? `${esc(lb)} 上方（不满足）` : `${esc(lb)} 下方`}，
      当日符合全部条件的股票为 <b>${r.latest_n}</b> 只。
      这是策略的正常行为——熊市过滤生效时本来就不该有信号。<br>
      <span style="color:#8d94a3">⚠️ 这里的「市场」是<b>自建${esc(r.mkt_index_name || '全A等权')}净值</b>，
      与上证/沪深300 等交易所指数不是同一条线 —— 分歧时请看下方「市场环境」表。</span></div>`;
  }
  let h = fb + `<div class="kv">
    <span>候选日 <b>${r.asof || r.date}</b></span>
    <span>符合条件 <b>${NUM(r.n)}</b> 只</span>
    <span>该日市场 <b style="font-family:inherit">${mkt}</b></span>
    <span>市场距MA60 <b>${F(r.mkt_dist,2,'%')}</b></span>
    <span>Breadth <b>${F(r.breadth,1,'%')}</b></span>
    <span>市场HV20 <b>${F(r.hv20,1,'%')}</b></span>
    <span style="color:#8d94a3">耗时 ${r.ms} ms</span></div>`
    + mktBlock(mk);

  if (!r.items || !r.items.length) {
    h += `<div class="empty">当前参数下，最近 ${r.latest_date} 及回溯范围内均无符合条件的股票。<br>
      可放宽「距MA60」分位、提高市值上限，或把市场环境改为「不限」。</div>`;
    $('#p-scan').innerHTML = h; return;
  }

  h += `<div class="card"><h3>候选股清单 <span class="tag">${r.asof || r.date} · 按得分降序 · 显示 ${r.shown}/${NUM(r.n)}</span></h3>
    <div class="desc">得分 = 距MA60 深度×6 + ret40 深度×3 + 小市值程度×4。
      这是<b>关注优先级</b>，不是预测涨幅——报告已证明「越极端越好」，所以越靠前越值得盯。</div>
    <div class="btnrow" style="max-width:260px;margin-bottom:11px">
      <button class="btn ghost" id="exp">导出 CSV</button>
    </div>
    <div class="tw"><table><thead><tr>
      <th class="sorter" data-k="score">#</th>
      <th class="sorter" data-k="code" style="text-align:left">代码</th>
      <th class="sorter" data-k="name" style="text-align:left">名称</th>
      <th class="sorter" data-k="ind" style="text-align:left">行业</th>
      <th class="sorter" data-k="close">收盘</th>
      <th class="sorter" data-k="dist60">距MA60</th>
      <th class="sorter" data-k="ret20">ret20</th>
      <th class="sorter" data-k="ret60">ret60</th>
      <th class="sorter" data-k="d_px60">距MA60分位</th>
      <th class="sorter" data-k="d_ret40">ret40分位</th>
      <th class="sorter" data-k="size_grp">市值组</th>
      <th class="sorter" data-k="board">板块</th>
      <th class="sorter" data-k="hv20">HV20</th>
      <th class="sorter" data-k="rvol">量比</th>
      <th class="sorter" data-k="fin_rev">营收</th>
      <th class="sorter" data-k="fin_rev_yoy">营收同比</th>
      <th class="sorter" data-k="fin_np">归母净利</th>
      <th class="sorter" data-k="fin_np_yoy">归母同比</th>
      <th>财报期</th>
      <th class="sorter" data-k="score">得分</th>
      <th>次日可买</th>
    </tr></thead><tbody id="scanbody"></tbody></table></div>
    <div class="cap">市值组 M1~M10：M1 = 最小 10%，M10 = 最大 10%。分位：1 = 最弱，10 = 最强。
      板块经涨跌停幅度交叉验证（主板 10% / 创业板·科创板 20% / 北交所 30%）。
      「次日可买」= 信号次日开盘未涨停（可实际买入）。
      财务列为<b>该候选日当时已公告</b>的财报（累计口径，同比增长按去年同期对比），
      即回测中实际使用的数值。<span style="color:#0d7a3a">动态对齐已生效</span>。</div></div>`;

  $('#p-scan').innerHTML = h;
  renderScanRows(r.items);
  $$('#p-scan th.sorter').forEach(th => th.addEventListener('click', () => {
    const k = th.dataset.k;
    r._sk = r._sk === k ? null : k;
    renderScanRows(r.items);
  }));
  $('#exp').addEventListener('click', async () => {
    const resp = await fetch('/api/export', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ params: collect(), pool: currentPool(), limit: 2000 })
    });
    const blob = await resp.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `超跌反转候选股_${r.asof || r.date}.csv`;
    document.body.appendChild(a); a.click(); a.remove();
  });
}

function renderScanRows(items) {
  const tb = $('#scanbody'); if (!tb) return;
  let arr = items.slice();
  const k = (SCAN && SCAN._sk) || null;
  if (k) arr.sort((a, b) => {
    const x = a[k], y = b[k];
    if (typeof x === 'string') return x.localeCompare(y);
    return (y ?? -1e9) - (x ?? -1e9);
  });
  tb.innerHTML = arr.map((it, i) => {
    const dp = it.d_px60;
    const pcls = dp === 1 ? 'd1' : dp === 2 ? 'd2' : dp === 3 ? 'd3' : 'dn';
    const rk = i === 0 ? 'rk g' : i < 3 ? 'rk s' : 'rk';
    return `<tr>
      <td class="l"><span class="${rk}">${i + 1}</span></td>
      <td class="l">${it.code}</td>
      <td class="nm">${it.name}</td>
      <td class="l" style="color:#5c6270">${it.ind}</td>
      <td>${F(it.close, 2)}</td>
      <td class="${cls(it.dist60)}">${F(it.dist60, 2, '%')}</td>
      <td class="${cls(it.ret20)}">${F(it.ret20, 2, '%')}</td>
      <td class="${cls(it.ret60)}">${F(it.ret60, 2, '%')}</td>
      <td><span class="pill ${pcls}">D${it.d_px60 ?? '—'}</span></td>
      <td>D${it.d_ret40 ?? '—'}</td>
      <!-- 市值组 = 分位序号（0 起），用 M 前缀区别于 D 档位 -->
      <td style="color:#5c6270">${it.size_grp == null ? '—' : 'M' + (it.size_grp + 1)}</td>
      <td>${it.board ? `<span class="pill bd${it.board}">${BD(it.board)}</span>` : '—'}</td>
      <td>${F(it.hv20, 1, '%')}</td>
      <td>${F(it.rvol, 2)}</td>
      <td style="color:#5c6270">${MONEY(it.fin_rev)}</td>
      <td class="${cls(it.fin_rev_yoy)}">${F(it.fin_rev_yoy, 1, '%')}</td>
      <td style="color:#5c6270">${MONEY(it.fin_np)}</td>
      <td class="${cls(it.fin_np_yoy)}">${F(it.fin_np_yoy, 1, '%')}</td>
      <td style="color:#8d94a3;font-size:11px">${it.fin_end ? it.fin_end.slice(2, 7) + ' Q' + (it.fin_q || '') : '—'}</td>
      <td style="font-weight:600;color:#1d4c85">${F(it.score, 1)}</td>
      <td>${it.can_buy ? '<span style="color:#17764f">是</span>'
        : '<span style="color:#a83232">否</span>'}</td></tr>`;
  }).join('');
}

/* ---------------- 渲染：回测 ---------------- */
/* ---------------- 渲染：容量约束块（实盘可执行性） ---------------- */
/* 两种口径：
     · 已投资金 —— 日收益 = Σ持仓收益 / 持仓只数，可与不限仓位基线直接对比
     · 账户资金 —— 日收益 = Σ持仓收益 / 同时持仓上限，是账户里真实看到的收益率
   两者比值 = 资金利用率。 */
function capHTML(r) {
  const c = r.capacity;
  if (!c) {
    return `<div class="card"><h3>仓位约束 <span class="tag">实盘可执行性</span></h3>
      <div class="desc">上方所有指标都是<b>不限仓位</b>口径：某日有多少信号就等权买多少只。
        实测平均同时持仓 <b>${F(r.avg_holdings, 0)} 只</b>
        （信号 ${NUM(r.n_signal)} 个 / 成交 ${NUM(r.n_trade)} 笔），个人资金无法复制。</div>
      <div class="warnbox" style="margin-top:2px">
        想看到「如果只能同时拿 10 只、每天最多买 3 只」的结果，
        请在左侧 <b>⑧ 仓位约束</b> 里把口径切到「限制持仓」后重新运行回测；
        也可以在那里把<b>建仓节奏</b>选成 <b>阶梯建仓</b>（如 1/2/2/2/3 只：
        第 1 批买 1 只、第 2 批 2 只…共 10 只满仓 —— 同时持仓与每日买入都由序列推出）。</div></div>`;
  }
  const util = c.avg_pos / c.max_pos;          // 资金利用率（平均持仓 / 上限）
  const lad = c.ladder;                        // 阶梯只数序列（等权时为 null）
  const zline = (c.z !== undefined && c.z !== null)
    ? `落在随机分布第 <b>${F(c.rand_pctile * 100, 1, '%')}</b> 百分位
       · z = <b>${F(c.z, 2)}</b> · 对照 ${NUM(c.n_sim)} 次随机选股`
    : '';
  let h = `<div class="card"><h3>仓位约束结果 <span class="tag">实盘可执行口径</span></h3>
    <div class="desc">同时持仓上限 <b>${c.max_pos}</b> 只 · ${lad
      ? `每日买入 <b>由建仓节奏推出</b>（每批 ${lad.join('/')} 只）`
      : `每日最多买入 <b>${c.max_new}</b> 只`} ·
      超额时按「<b>${c.pick_name}</b>」排序（${c.pick_desc}）。`
    + (lad ? `<br>建仓节奏：<b>阶梯建仓</b> 每批 ${lad.join(' / ')} 只 ——
        满仓 <b>${c.ladder_total}</b> 只（= 序列之和 = 同时持仓上限），每只等分
        ${F(100 / c.ladder_total, 1, '%')} 资金；每次建仓把持仓补到下一个累计目标，买满即停。`
      : '') + `</div>

    <div class="vs" style="margin-bottom:13px">
      <div class="side">
        <div class="sh">已投资金口径</div>
        <div class="big ${cls(c.cagr)}">${PCT(c.cagr)}</div>
        <div class="sub">假设闲置资金也赚钱，仅用于与基线对比</div>
        <div style="margin-top:9px">
          <div class="l2"><span>Sharpe</span><b>${F(c.sharpe, 3)}</b></div>
          <div class="l2"><span>最大回撤</span><b>${PCT(c.mdd)}</b></div>
          <div class="l2"><span>年化波动</span><b>${PCT(c.vol)}</b></div>
        </div>
      </div>
      <div class="mid">利用率 ${F(util * 100, 0, '%')}</div>
      <div class="side hl2">
        <div class="sh">账户资金口径 · 你真实看到的</div>
        <div class="big ${cls(c.cap_cagr)}">${PCT(c.cap_cagr)}</div>
        <div class="sub">${lad
          ? `每只等分资金（满仓 ${c.ladder_total} 只），没投出去的部分赚 0`
          : `资金按 ${c.max_pos} 份等分，空着的那份赚 0`}</div>
        <div style="margin-top:9px">
          <div class="l2"><span>Sharpe</span><b>${F(c.cap_sharpe, 3)}</b></div>
          <div class="l2"><span>最大回撤</span><b>${PCT(c.cap_mdd)}</b></div>
          <div class="l2"><span>年化波动</span><b>${PCT(c.cap_vol)}</b></div>
        </div>
      </div>
    </div>

    <div class="mgrid" style="margin-bottom:12px">
      <div class="mc"><div class="l">实际建仓</div>
        <div class="v sm">${NUM(c.n_hold)}</div>
        <div class="n">信号 ${NUM(c.n_signal)} 个</div></div>
      <div class="mc"><div class="l">被迫丢弃</div>
        <div class="v sm neg">${PCT(c.drop_pct, 1)}</div>
        <div class="n">成交率 ${PCT(c.fill_pct, 1)}</div></div>
      <div class="mc"><div class="l">平均持仓只数</div>
        <div class="v sm">${F(c.avg_pos, 1)}</div>
        <div class="n">上限 ${c.max_pos} 只 · 实达 ${c.max_pos_seen} 只</div></div>
      <div class="mc"><div class="l">${lad ? '平均仓位' : '资金利用率'}</div>
        <div class="v sm">${lad ? PCT(c.avg_invested, 1) : PCT(util, 1)}</div>
        <div class="n">${hasNum(c.full_pct)
          ? `满仓日占比 ${PCT(c.full_pct, 1)}`
          : `有持仓日占比 ${PCT(c.active_pct, 1)}`}</div></div>
      <div class="mc"><div class="l">空仓日占比</div>
        <div class="v sm">${PCT(c.empty_pct, 1)}</div>
        <div class="n">无信号可买的天数</div></div>
    </div>`;

  // 随机基准对照（判断「选股规则」是否真有效）
  if (c.z !== undefined && c.z !== null) {
    const stronger = c.z > 2;
    h += `<div class="card" style="background:#fafbfc;margin-bottom:0">
      <h3 style="font-size:13px">选股规则有效性检验 <span class="tag">对照 ${NUM(c.n_sim)} 次随机选股</span></h3>
      <div class="desc">同样受 ${c.max_pos} 只${lad
        ? `、每批 ${lad.join('/')} 只`
        : ` / 日 ${c.max_new} 只`}约束，但<b>随机</b>挑股票，
        重复 ${NUM(c.n_sim)} 次得到分布：账户口径 CAGR 均值 <b>${PCT(c.rand_mean)}</b>，
        标准差 ${PCT(c.rand_sd, 2)}，P5~P95 为 ${PCT(c.rand_p05)} ~ ${PCT(c.rand_p95)}，
        区间 ${PCT(c.rand_min)} ~ ${PCT(c.rand_max)}。</div>
      <div class="warnbox" style="margin:0;background:${stronger ? '#eaf6ef' : '#fdf1e0'};
        border-color:${stronger ? '#bfe6cd' : '#f0d9b5'};color:${stronger ? '#0d7a3a' : '#a8622c'}">
        「${c.pick_name}」账户口径 CAGR <b>${PCT(c.cap_cagr)}</b>，${zline}。
        ${stronger ? '✅ 显著优于随机选股 —— 这个「先买哪只」的规则确实带来了超额。'
                   : '⚠️ 未显著优于随机，说明该规则没有额外信息量，建议改用「最超跌优先」。'}
      </div></div>`;
  }
  h += `<div class="cap">口径说明：<b>已投资金</b>把每笔投入的资金等权看待，
    可与上方不限仓位基线直接比较；<b>账户资金</b>${lad
      ? `把资金按满仓 ${c.ladder_total} 只等分（每只 ${F(100 / c.ladder_total, 1, '%')}），没投出去的部分赚 0`
      : `按 ${c.max_pos} 份等分，没投出去的部分赚 0`}，
    这才是你账户里真实看到的收益率${lad
      ? `。阶梯模式下平均仓位 ${F(c.avg_invested * 100, 1, '%')}`
      : `。两者之比就是资金利用率 ${F(util * 100, 0, '%')}`}。
    被丢弃的信号<b>不补买</b>（保守假设），因此结果偏保守、不含运气成分。</div></div>`;
  return h;
}

function renderBacktest(r) {
  const cs = r.conditions && r.conditions.length
    ? r.conditions.map(c => `<span class="c">${c}</span>`).join('')
    : '<span class="c none">无过滤 = 全市场基准</span>';
  const b = r.bench;
  let h = `<div class="condlist">${cs}</div>`;

  h += `<div class="mgrid" style="margin-bottom:14px">
    <div class="mc hl"><div class="l">年化收益 CAGR</div>
      <div class="v ${cls(r.cagr)}">${PCT(r.cagr)}</div>
      <div class="n">算术年化 ${PCT(r.ann_arith)}</div></div>
    <div class="mc"><div class="l">最大回撤</div>
      <div class="v neg">${PCT(r.mdd)}</div>
      <div class="n">${
        b ? '基准 ' + PCT(b.mdd) : ''}</div></div>
    <div class="mc"><div class="l">Sharpe</div>
      <div class="v">${F(r.sharpe, 2)}</div>
      <div class="n">年化波动 ${PCT(r.vol)}</div></div>
    <div class="mc"><div class="l">胜率</div>
      <div class="v ${r.win > .5 ? 'pos' : 'neg'}">${PCT(r.win)}</div>
      <div class="n">单笔均值 ${PCT(r.mean, 2)}</div></div>
    <div class="mc"><div class="l">盈亏比</div>
      <div class="v">${F(r.payoff, 2)}</div>
      <div class="n">中位数 ${PCT(r.median, 2)}</div></div>
    <div class="mc"><div class="l">Profit Factor</div>
      <div class="v">${F(r.pf, 2)}</div>
      <div class="n">P10 ${PCT(r.p10, 1)} · P90 ${PCT(r.p90, 1)}</div></div>
    <div class="mc"><div class="l">信号 / 交易数</div>
      <div class="v sm">${NUM(r.n_signal)}</div>
      <div class="n">实际成交 ${NUM(r.n_trade)} 笔</div></div>
    <div class="mc"><div class="l">日均持仓</div>
      <div class="v sm">${F(r.avg_holdings, 0)}</div>
      <div class="n">有仓位天数占比 ${PCT(r.active_pct, 1)}</div></div>
  </div>`;

  // ---- 容量约束结果（实盘可执行性）
  h += capHTML(r);

  // 超额表
  const HS = [1, 5, 10, 20, 40, 60];
  h += `<div class="card"><h3>分持有期超额收益 <span class="tag">日加权口径</span></h3>
    <div class="desc">exw = 先按日求组合均值再减市场基准，然后对日度序列做 t 检验。
      这是判断因子是否真实有效的核心口径，比观测加权均值可靠得多。</div>
    <div class="tw"><table><thead><tr><th style="text-align:left">持有期</th>
      ${HS.map(H => `<th>${H} 日</th>`).join('')}</tr></thead><tbody>
      <tr><td class="l">日度超额</td>
        ${HS.map(H => `<td class="${cls(r.exw[H])}">${F(r.exw[H] * 100, 3, '%')}</td>`).join('')}</tr>
      <tr><td class="l">t 值</td>
        ${HS.map(H => `<td class="${(r.exw_t[H] || 0) > 2 ? 'pos' : (r.exw_t[H] || 0) < -2 ? 'neg' : 'neu'}">${F(r.exw_t[H], 2)}</td>`).join('')}</tr>
    </tbody></table></div>
    <div class="cap">|t| &gt; 2 视为统计显著。全负说明「等确认」是在等已经涨完的部分。</div></div>`;

  // 图表
  const CC = r.capacity;
  h += `<div class="charts">
    <div class="card" style="margin:0"><h3>净值曲线 <span class="tag">含 0.3% 双边成本</span></h3>
      <div class="chart tall" id="ch-nav"></div>
      <div class="cap" id="navcap">对数坐标。策略 vs 同股票池等权基准${
        CC ? `；<b>紫色</b>为受限仓位（账户资金口径，${CC.max_pos}只/日${CC.max_new}只）` : ''}。</div></div>
    <div class="card" style="margin:0"><h3>回撤曲线</h3>
      <div class="chart tall" id="ch-dd"></div></div>
  </div>
  <div class="charts" style="margin-top:14px">
    <div class="card" style="margin:0"><h3>逐年收益 <span class="tag">含成本</span></h3>
      <div class="chart" id="ch-year"></div></div>
    <div class="card" style="margin:0"><h3>四段分期</h3>
      <div class="chart" id="ch-seg"></div></div>
  </div>`;

  // 分期表
  const SK = ['2015-2020', '2021-2022', '2023-2024', '2025-2026'];
  const capSeg = (CC && CC.segs) ? CC.segs : null;
  h += `<div class="card"><h3>样本内 / 样本外</h3>
    <div class="tw"><table><thead><tr><th style="text-align:left">口径</th>
      ${SK.map(k => `<th>${k}</th>`).join('')}<th>IS 2015-2020</th><th>OOS 2021-2026</th>
    </tr></thead><tbody><tr><td class="l">不限仓位</td>
      ${SK.map(k => `<td class="${cls(r.segs[k])}">${PCT(r.segs[k])}</td>`).join('')}
      <td class="${cls(r.segs['IS(2015-2020)'])}">${PCT(r.segs['IS(2015-2020)'])}</td>
      <td class="${cls(r.segs['OOS(2021-2026)'])}">${PCT(r.segs['OOS(2021-2026)'])}</td>
    </tr>${
      capSeg ? `<tr><td class="l">受限 ${CC.max_pos}只/日${CC.max_new}只<br>
        <span style="font-size:10.5px;color:var(--tx3)">账户资金口径</span></td>
        ${SK.map(k => `<td class="${cls(capSeg[k])}">${PCT(capSeg[k])}</td>`).join('')}
        <td class="${cls(capSeg['IS(2015-2020)'])}">${PCT(capSeg['IS(2015-2020)'])}</td>
        <td class="${cls(capSeg['OOS(2021-2026)'])}">${PCT(capSeg['OOS(2021-2026)'])}</td>
      </tr>` : ''}</tbody></table></div>
    <div class="cap">报告提醒：2015-2026 恰好是小市值占优期，OOS 极强不代表未来。
      「距MA60 D1 + 大市值」只有 2.9% 年化 —— 没有小市值，超跌几乎不赚钱。</div></div>`;

  $('#p-bt').innerHTML = h;

  // --- 净值图
  const mult = v => Math.log(Math.max(v, 1e-6));
  const series = [{
    name: '策略（含成本）', type: 'line', showSymbol: false,
    data: r.nav.map(mult), lineStyle: { width: 2, color: '#3266ad' }, itemStyle: { color: '#3266ad' },
    areaStyle: { color: 'rgba(50,102,173,.07)' }
  }, {
    name: '策略（毛收益）', type: 'line', showSymbol: false,
    data: r.nav_gross.map(mult), lineStyle: { width: 1.2, color: '#8fb4dd', type: 'dashed' },
    itemStyle: { color: '#8fb4dd' }
  }];
  if (b && b.nav) series.push({
    name: '同池等权基准', type: 'line', showSymbol: false,
    data: b.nav.map(mult), lineStyle: { width: 1.4, color: '#b0b5bf' },
    itemStyle: { color: '#b0b5bf' }
  });
  // 受限仓位：账户资金口径（实盘真实感受）——放最后，视觉上最突出
  if (CC && CC.nav_cap) series.push({
    name: `受限仓位 ${CC.max_pos}只/日${CC.max_new}只（账户口径）`,
    type: 'line', showSymbol: false,
    data: CC.nav_cap.map(mult), lineStyle: { width: 2, color: '#7c4dbb' },
    itemStyle: { color: '#7c4dbb' }
  });
  const nc = mkChart('ch-nav');
  nc && nc.setOption({
    grid: { left: 54, right: 16, top: 34, bottom: 26 },
    tooltip: { trigger: 'axis', ...TIP,
      formatter: p => { const i = p[0].dataIndex;
        return `<b>${r.dates[i]}</b><br>` + p.map(s =>
          `${s.marker}${s.seriesName}: <b>${Math.exp(s.value).toFixed(2)}×</b>`).join('<br>'); } },
    legend: { top: 4, right: 8, itemWidth: 12, itemHeight: 8,
      textStyle: { fontSize: 11, color: '#5c6270' } },
    xAxis: { type: 'category', data: r.dates, boundaryGap: false, ...AX,
      axisLabel: { ...AX.axisLabel, formatter: v => String(v).slice(0, 7),
        interval: Math.max(1, Math.floor(r.dates.length / 8)) } },
    yAxis: { type: 'value', ...AX, axisLabel: { ...AX.axisLabel,
      formatter: v => Math.exp(v).toFixed(1) + '×' } },
    series
  });

  // --- 回撤图
  let peak = 1, dds = [];
  r.nav.forEach(v => { peak = Math.max(peak, v); dds.push((v / peak - 1) * 100); });
  const dc = mkChart('ch-dd');
  dc && dc.setOption({
    grid: { left: 54, right: 16, top: 22, bottom: 26 },
    tooltip: { trigger: 'axis', ...TIP,
      formatter: p => `<b>${r.dates[p[0].dataIndex]}</b><br>回撤: <b>${p[0].value.toFixed(2)}%</b>` },
    xAxis: { type: 'category', data: r.dates, boundaryGap: false, ...AX,
      axisLabel: { ...AX.axisLabel, formatter: v => String(v).slice(0, 7),
        interval: Math.max(1, Math.floor(r.dates.length / 8)) } },
    yAxis: { type: 'value', max: 0, ...AX,
      axisLabel: { ...AX.axisLabel, formatter: v => v.toFixed(0) + '%' } },
    series: [{ type: 'line', showSymbol: false, data: dds,
      lineStyle: { width: 1.4, color: '#e04a4a' }, itemStyle: { color: '#e04a4a' },
      areaStyle: { color: 'rgba(224,74,74,.12)' } }]
  });

  // --- 年度
  const YC = r.yearly || [];
  const yc = mkChart('ch-year');
  yc && yc.setOption({
    grid: { left: 48, right: 12, top: 24, bottom: 26 },
    tooltip: { trigger: 'axis', ...TIP,
      formatter: p => `<b>${p[0].name}</b><br>` + p.map(s =>
        `${s.marker}${s.seriesName}: <b>${s.value.toFixed(1)}%</b>`).join('<br>') },
    legend: (CC && CC.yearly) ? { top: 0, right: 4, itemWidth: 11, itemHeight: 8,
      textStyle: { fontSize: 10.5, color: '#5c6270' } } : undefined,
    xAxis: { type: 'category', data: YC.map(x => String(x.year)), ...AX },
    yAxis: { type: 'value', ...AX,
      axisLabel: { ...AX.axisLabel, formatter: v => v + '%' } },
    series: [{
      name: '不限仓位', type: 'bar', barMaxWidth: 22,
      data: YC.map(x => ({ value: +(x.ret * 100).toFixed(1),
        itemStyle: { color: x.ret >= 0 ? '#e04a4a' : '#2ea06a',
          borderRadius: [3, 3, 0, 0] } })),
      label: { show: !(CC && CC.yearly), position: 'top', fontSize: 10, color: '#5c6270',
        formatter: p => p.value.toFixed(0) + '%' }
    }].concat((CC && CC.yearly) ? [{
      name: `受限 ${CC.max_pos}只`, type: 'bar', barMaxWidth: 22,
      data: YC.map(x => {
        const m = (CC.yearly || []).find(y => y.year === x.year);
        const v = m ? +(m.ret * 100).toFixed(1) : 0;
        return { value: v, itemStyle: { color: v >= 0 ? '#c58fd6' : '#7fb7a4',
          borderRadius: [3, 3, 0, 0] } };
      }),
      label: { show: true, position: 'top', fontSize: 9.5, color: '#5c6270',
        formatter: p => p.value.toFixed(0) + '%' }
    }] : [])
  });

  // --- 分期
  const sc = mkChart('ch-seg');
  sc && sc.setOption({
    grid: { left: 48, right: 12, top: 24, bottom: 26 },
    tooltip: { trigger: 'axis', ...TIP,
      formatter: p => `<b>${p[0].name}</b><br>累计收益: <b>${p[0].value.toFixed(1)}%</b>` },
    xAxis: { type: 'category', data: SK, ...AX },
    yAxis: { type: 'value', ...AX,
      axisLabel: { ...AX.axisLabel, formatter: v => v + '%' } },
    series: [{
      type: 'bar', barMaxWidth: 62,
      data: SK.map(k => ({ value: +(((r.segs[k] ?? 0)) * 100).toFixed(1),
        itemStyle: { color: (r.segs[k] ?? 0) >= 0 ? '#e04a4a' : '#2ea06a',
          borderRadius: [3, 3, 0, 0] } })),
      label: { show: true, position: 'top', fontSize: 11, color: '#5c6270',
        formatter: p => p.value.toFixed(1) + '%' }
    }]
  });
}

/* ---------------- 渲染：交易明细 ---------------- */
let TR = null;          // 最近一次交易明细响应
const TST = { page: 1, page_size: 50, sort: 'exit_date', order: 'desc',
              win: null, year: null, code: '' };
// 排序字段 ↔ 中文表头（点击表头时切换）
const TCOLS = [
  ['seq', '#', 1, 'rk2'],
  ['code', '代码', 1, 'l'],
  ['name', '名称', 1, 'nm'],
  ['ind', '行业', 1, 'l'],
  ['board', '板块', 0],
  ['signal_date', '信号日', 0],
  ['entry_date', '买入日', 0],
  // 阶梯建仓时该笔占账户资金的比例（等权不限仓位时为 null，显示 —）
  ['w', '仓位', 0],
  ['exit_date', '卖出日', 0],
  ['buy', '买入价', 0],
  ['sell', '卖出价', 0],
  ['net', '净收益', 0],
  ['ret', '毛收益', 0],
  ['bench', '同期基准', 0],
  ['excess', '超额', 0],
  ['size_grp', '市值组', 0],
  ['d_px60', '距MA60', 0],
  ['fin_rev', '营收', 0],
  ['fin_rev_yoy', '营收同比', 0],
  ['fin_np', '归母净利', 0],
  ['fin_np_yoy', '归母同比', 0],
];

async function runTrades() {
  const el = $('#p-trades');
  const capped = segv('#seg-cap') === '1';
  el.innerHTML = `<div class="card"><div class="empty">
    <span class="spin"></span> 正在提取逐笔交易明细（${
      capped ? '已开启仓位约束，只列实际建仓的那批' : '10 万笔量级'}，约 3~8 秒）…</div></div>`;
  try {
    const body = {
      params: collect(), pool: currentPool(), cost: 0.003,
      page: TST.page, page_size: TST.page_size,
      sort: TST.sort, order: TST.order,
    };
    if (TST.win !== null) body.win = TST.win;
    if (TST.year) body.year = TST.year;
    if (TST.code) body.code = TST.code;
    const r = await api('/api/trades', body);
    if (r.error) { el.innerHTML = `<div class="errbox"><b>无法列出交易：</b>${r.error}</div>`; return; }
    TR = r;
    renderTrades(r);
  } catch (e) {
    el.innerHTML = `<div class="errbox"><b>交易明细加载失败：</b>${e.message}</div>`;
  }
}

/* ---- 交易明细口径说明：受容量约束 / 不限仓位 两种文案 ---- */
function tradePlanNote(p) {
  const lad = p.ladder;
  return `<span style="color:#6b3fa0">📌 本页是<b>仓位约束</b>口径 ——
    ${lad
      ? `建仓节奏 <b>阶梯建仓</b>（每批 ${lad.join('/')} 只，满仓 ${p.ladder_total} 只）`
        + `、每日买入由序列推出、冲突时按「<b>${p.pick_name}</b>」排序优先买入。`
        + `每只等分资金，比例见明细表的「<b>仓位</b>」列。`
      : `同时最多持 <b>${p.max_pos} 只</b>、每日最多买 <b>${p.max_new} 只</b>、`
        + `冲突时按「<b>${p.pick_name}</b>」排序优先买入。`}
    列出的是这套规则下<b>实际会建仓</b>的交易（实盘可照抄执行）。</span>`;
}
function tradeFreeNote() {
  return `<span style="color:#a8622c">⚠️ 本页是<b>不限仓位</b>口径 ——
    列出的是<b>全部</b>符合条件的信号对应的交易（实盘做不完这么多）。
    若想看「只能同时拿 N 只、每天最多买 M 只」时<b>实际会发生</b>哪些交易，
    请在左侧 <b>⑧ 仓位约束</b> 打开限制后再回测。</span>`;
}
function tradePlanCard(p, hold) {
  const ns = Math.max(p.n_signal, 1);
  const keepPct = Math.min(1, (p.n_plan || 0) / ns);
  const lad = p.ladder;
  return `<div class="card" style="background:#fafbfc">
    <h3 style="font-size:13px">仓位约束诊断 <span class="tag">与「策略回测」页同口径</span></h3>
    ${lad ? `<div class="cap" style="margin:0 0 10px">
      建仓节奏：<b>阶梯建仓</b> 每批 ${lad.join(' / ')} 只 —— 满仓 <b>${p.ladder_total}</b> 只
      （= 序列之和 = 同时持仓上限），每只等分 <b>${(100 / p.ladder_total).toFixed(1)}%</b> 资金；
      每次建仓把持仓补到下一个累计目标，买满即停。
      明细表「<b>仓位</b>」列即该笔占账户资金的比例。</div>` : ''}
    <div class="mgrid" style="margin-bottom:10px">
      <div class="mc hl"><div class="l">本次实际建仓</div>
        <div class="v sm">${NUM(p.n_plan)} 笔</div>
        <div class="n">占全部信号 ${PCT(keepPct, 2)}</div></div>
      <div class="mc"><div class="l">被丢弃信号</div>
        <div class="v sm">${NUM(p.n_drop)}</div>
        <div class="n">占 ${PCT(p.drop_pct, 1)}（不补买）${p.n_drop_dup > 0 ?
          `<br>其中 ${NUM(p.n_drop_dup)} 笔因已持有未平仓` : ''}</div></div>
      <div class="mc"><div class="l">本页可结算</div>
        <div class="v sm">${NUM(p.n_plan - p.n_open)} 笔</div>
        <div class="n">扣掉未平仓</div></div>
      <div class="mc"><div class="l">期末未平仓</div>
        <div class="v sm">${NUM(p.n_open)} 笔</div>
        <div class="n">持有期尚未走完</div></div>
    </div>
    <div class="cap" style="margin:0">
      ${p.n_open > 0 ? `⚠️ 有 <b>${p.n_open}</b> 笔建仓没有结局，` +
        `本页<b>明细表下方的「未结束交易」区块</b>把它们的每一笔都列出来了` +
        `（<b>不计入</b>完成交易数与胜率统计 —— 否则会因缺少结局而偏乐观）：<br>` +
        `<b>· 持有中</b>：买入后持有期<b>还没走完就到数据边界了</b>，` +
        `实盘里就是<b>还拿在手上</b>；<br>` +
        `<b>· T+1 买不进</b>：信号日次日<b>一字涨停/停牌</b>，开盘买不到，` +
        `这笔<b>从未建仓</b>（信号作废，不算亏损）。<br>` : ''}
      规则：<b>${p.pick_name}</b>；信号拥挤时只在当日信号里按该规则取前 ${p.max_new} 只，
      ${lad ? `累计投入满 <b>${p.ladder_total}</b> 成` : `持仓满 ${p.max_pos} 只`}时不再新开，
      被丢弃的信号<b>不补买</b>（保守假设）；
      <b>同一只票未平仓期间不重复买入</b>，卖出后才回到候选池。
    </div></div>`;
}

function renderTrades(r) {
  const s = r.summary, f = r.filtered_summary;
  const cs = r.conditions && r.conditions.length
    ? r.conditions.map(c => `<span class="c">${c}</span>`).join('')
    : '<span class="c none">无过滤 = 全市场基准</span>';
  let h = `<div class="condlist">${cs}</div>`;

  // ---- 汇总卡片
  h += `<div class="mgrid" style="margin-bottom:14px">
    <div class="mc hl"><div class="l">完成交易</div>
      <div class="v sm">${NUM(s.n_trade)} 笔</div>
      <div class="n">${NUM(s.n_stock)} 只股票</div></div>
    <div class="mc"><div class="l">单笔胜率</div>
      <div class="v ${s.win > .5 ? 'pos' : 'neg'}">${PCT(s.win)}</div>
      <div class="n">盈利 ${NUM(Math.round(s.n_trade * s.win))} 笔</div></div>
    <div class="mc"><div class="l">单笔均值</div>
      <div class="v ${cls(s.mean)}">${PCT(s.mean, 2)}</div>
      <div class="n">中位数 ${PCT(s.median, 2)}</div></div>
    <div class="mc"><div class="l">盈亏比 / PF</div>
      <div class="v">${F(s.payoff, 2)}<span style="font-size:13px;color:var(--tx3)">
        / ${F(s.pf, 2)}</span></div>
      <div class="n">标准差 ${PCT(s.std, 1)}</div></div>
    <div class="mc"><div class="l">最佳 / 最差</div>
      <div class="v sm"><span class="pos">${PCT(s.best, 0)}</span>
        <span style="color:var(--tx3)">/</span>
        <span class="neg">${PCT(s.worst, 0)}</span></div>
      <div class="n">P10 ${PCT(s.p10, 1)} · P90 ${PCT(s.p90, 1)}</div></div>
    <div class="mc"><div class="l">平均超额</div>
      <div class="v ${cls(s.avg_excess)}">${PCT(s.avg_excess, 2)}</div>
      <div class="n">跑赢基准 ${PCT(s.excess_win, 1)} 的笔数</div></div>
    <div class="mc"><div class="l">持有期</div>
      <div class="v sm">${r.hold} 日</div>
      <div class="n">单边成本 ${(r.cost * 100).toFixed(2)}% · 双边 ${(r.cost * 200).toFixed(2)}%</div></div>
    <div class="mc"><div class="l">交易区间</div>
      <div class="v sm" style="font-size:13px">${s.date_start || '—'}</div>
      <div class="n">至 ${s.date_end}</div></div>
    ${r.n_open > 0 ? `<div class="mc" style="background:#fffbf0;border:1px solid #f0e2c0">
      <div class="l">未结束（不计入统计）</div>
      <div class="v sm" style="color:#a8622c">${NUM(r.n_open)} 笔</div>
      <div class="n">${r.n_open_holding} 持有中 · ${r.n_open_nobuy} 买不进${r.n_open_stuck ? ' · ' + r.n_open_stuck + ' 卖出日无价' : ''}</div></div>` : ''}
  </div>`;

  // ---- 口径说明
  h += `<div class="card"><h3>交易口径 <span class="tag">固定持有期 · 无前视偏差</span></h3>
    <div class="desc" style="margin-bottom:0">
      每一笔交易 = <b>信号日 T</b> 收盘后按条件选出 → <b>T+1 开盘</b>买入 →
      持有 <b>${r.hold} 个交易日</b> → <b>T+1+${r.hold} 开盘</b>卖出。
      净收益已扣 <b>${(r.cost * 200).toFixed(2)}%</b> 双边成本（印花税+佣金+滑点）。
      「同期基准」= 同口径（T+1开盘 → T+1+${r.hold}开盘）的全市场链式收益，
      「超额」= 净收益 − 基准。「营收 / 归母」列是该信号日<b>当时已公告</b>的财报
      （动态对齐，无前视偏差）。<br>
      <span style="color:#a8622c">⚠️ 这些交易<b>不是同时发生的</b>：按日度再平衡口径统计的组合
      CAGR 见「策略回测」页；本页逐笔收益是「每笔独立持有」的单笔口径，两者不可直接相乘。</span>
      <br>${r.plan ? tradePlanNote(r.plan) : tradeFreeNote()}
    </div></div>`;

  // ---- 仓位约束口径提示（仅受限口径显示）
  if (r.plan) h += tradePlanCard(r.plan, r.hold);

  // ---- 分布图 + 逐年图
  h += `<div class="charts">
    <div class="card" style="margin:0"><h3>单笔净收益分布
      <span class="tag">${r.hist.edges[0]}% ~ ${r.hist.edges[r.hist.edges.length-1]}%</span></h3>
      <div class="chart" id="ch-hist"></div>
      <div class="cap">横轴为净收益率分箱，纵轴为笔数。长期右偏是多头交易策略的典型形态。</div></div>
    <div class="card" style="margin:0"><h3>逐年交易表现 <span class="tag">含双边成本</span></h3>
      <div class="chart" id="ch-tyear"></div>
      <div class="cap">柱 = 该年单笔平均净收益；折线 = 该年单笔胜率。样本少（&lt;30 笔）年份请谨慎解读。</div></div>
  </div>`;

  // ---- 逐年表
  if ((r.yearly || []).length) {
    h += `<div class="card"><h3>逐年交易统计</h3>
      <div class="tw"><table><thead><tr>
        <th style="text-align:left">年份</th><th>交易数</th><th>盈利数</th><th>胜率</th>
        <th>平均净收益</th><th>中位数</th><th>累计</th><th>盈亏比</th>
        <th>最佳</th><th>最差</th></tr></thead><tbody>`;
    r.yearly.forEach(y => {
      h += `<tr><td class="l"><b>${y.year}</b></td><td>${NUM(y.n)}</td>
        <td>${NUM(y.n_win)}</td>
        <td class="${y.win > .5 ? 'pos' : 'neg'}">${PCT(y.win, 1)}</td>
        <td class="${cls(y.mean)}">${PCT(y.mean, 2)}</td>
        <td class="${cls(y.median)}">${PCT(y.median, 2)}</td>
        <td class="${cls(y.sum)}">${F(y.sum, 1)}</td>
        <td>${F(y.pf, 2)}</td>
        <td class="pos">${PCT(y.best, 1)}</td>
        <td class="neg">${PCT(y.worst, 1)}</td></tr>`;
    });
    h += `</tbody></table></div>
      <div class="cap">「累计」= 该年所有交易净收益<b>简单相加</b>（每股等权 1 份，非复利），
        用于比较各年的机会质量，不等于该年的组合收益率。</div></div>`;
  }

  // ---- 最佳 / 最差榜
  if ((r.best || []).length) {
    const tb = (arr, isBest) => arr.map(x => `<tr>
      <td class="l">${x.code}</td><td class="nm">${x.name}</td>
      <td style="color:var(--tx3)">${x.signal_date}</td>
      <td style="color:var(--tx3)">${x.exit_date || '—'}</td>
      <td class="${isBest ? 'pos' : 'neg'}">${PCTN(x.net, 1)}</td>
      <td class="${isBest ? 'pos' : 'neg'}">${PCTN(x.excess, 1)}</td></tr>`).join('');
    h += `<div class="charts" style="margin-top:14px">
      <div class="card" style="margin:0"><h3>最佳 20 笔 <span class="tag">按净收益</span></h3>
        <div class="tw"><table><thead><tr><th style="text-align:left">代码</th>
          <th style="text-align:left">名称</th><th style="text-align:left">信号日</th>
          <th style="text-align:left">卖出日</th><th>净收益</th><th>超额</th>
        </tr></thead><tbody>${tb(r.best, true)}</tbody></table></div>
        <div class="cap">超跌反转的收益高度集中于少数极端反弹（如 2024-09「924 行情」）。</div></div>
      <div class="card" style="margin:0"><h3>最差 20 笔 <span class="tag">按净收益</span></h3>
        <div class="tw"><table><thead><tr><th style="text-align:left">代码</th>
          <th style="text-align:left">名称</th><th style="text-align:left">信号日</th>
          <th style="text-align:left">卖出日</th><th>净收益</th><th>超额</th>
        </tr></thead><tbody>${tb(r.worst, false)}</tbody></table></div>
        <div class="cap">最差笔集中在「跌了还能再跌」的退市/暴雷股——小市值策略的主要风险来源。</div></div>
    </div>`;
  }

  // ---- 过滤器 + 明细表
  const yrs = (r.yearly || []).map(y => y.year);
  h += `<div class="card"><h3>逐笔交易明细
    <span class="tag">已结算 ${NUM(r.n_trade)} 笔 · 筛选后 ${NUM(r.n_filtered)} 笔${
      r.n_open > 0 ? ` · 另有 ${NUM(r.n_open)} 笔未结束（见下）` : ''}</span></h3>
    <div class="desc">每一行是一笔<b>已了结</b>的交易。点表头可排序，行底色：<span style="color:#a83232">浅红=亏损</span>。
      回测过程中<b>实际使用的财报</b>列在右侧（该信号日当时已公告的数值）。
      ${r.n_open > 0 ? `没有结局的 <b>${NUM(r.n_open)}</b> 笔不在这里，而在下方的
      <b>「未结束交易」</b>区块 —— 它们<b>不计入</b>任何统计。` : ''}</div>
    <div class="trbar">
      <span class="lb">盈亏</span>
      <span class="seg" id="t-win">
        <button data-w="" class="${TST.win === null ? 'on' : ''}">全部</button>
        <button data-w="1" class="${TST.win === true ? 'on' : ''}">仅盈利</button>
        <button data-w="0" class="${TST.win === false ? 'on' : ''}">仅亏损</button>
      </span>
      <span class="lb">年份</span>
      <select id="t-year"><option value="">全部</option>
        ${yrs.map(y => `<option value="${y}" ${String(TST.year) === String(y) ? 'selected' : ''}>${y}</option>`).join('')}
      </select>
      <span class="lb">个股</span>
      <input type="text" id="t-code" placeholder="600519" value="${TST.code || ''}">
      <span class="sp"></span>
      <select id="t-size">
        <option value="50" ${TST.page_size === 50 ? 'selected' : ''}>50 条/页</option>
        <option value="100" ${TST.page_size === 100 ? 'selected' : ''}>100 条/页</option>
        <option value="200" ${TST.page_size === 200 ? 'selected' : ''}>200 条/页</option>
      </select>
      <button class="mini" id="t-apply">应用筛选</button>
      <button class="mini" id="t-reset">清除</button>
      <button class="mini" id="t-exp">导出 CSV</button>
    </div>`;
  if (f && f.n !== undefined) {
    h += `<div class="hint2">筛选结果：<b>${NUM(f.n)}</b> 笔 ·
      胜率 <b class="${f.win > .5 ? 'pos' : 'neg'}">${PCT(f.win, 1)}</b> ·
      平均净收益 <b class="${cls(f.mean)}">${PCT(f.mean, 2)}</b> ·
      中位数 <b class="${cls(f.median)}">${PCT(f.median, 2)}</b> ·
      累计 <b class="${cls(f.sum)}">${F(f.sum, 1)}</b>${f.pf ? ` · PF <b>${F(f.pf, 2)}</b>` : ''}</div>`;
  }
  h += `<div class="tw" style="margin-top:11px"><table><thead><tr>
    ${TCOLS.map(([k, lbl]) => `<th class="ts" data-k="${k}" ${
      k === 'code' || k === 'name' || k === 'ind' ? 'style="text-align:left"' : ''}>${lbl}${
      r.sort === k ? `<span class="ar">${r.order === 'desc' ? '▼' : '▲'}</span>` : ''}</th>`).join('')}
    </tr></thead><tbody id="tbody-trades"></tbody></table></div>
    <div class="trow">
      <span>第 <b>${r.page}</b> / ${r.n_page} 页 ·
        显示 ${NUM((r.page - 1) * r.page_size + 1)}–${NUM((r.page - 1) * r.page_size + r.rows.length)} 条</span>
      <span class="pg" id="t-pg"></span>
    </div></div>`;

  // ---- 未结束交易（独立区块，**不计入上面的任何统计**）
  h += tradeOpenBlock(r);

  $('#p-trades').innerHTML = h;

  renderTradeRows(r.rows);
  drawTradeCharts(r);
  bindTradeControls(r);
}

/* 未结束交易区块。
   ⚠️ 两条纪律：
     1. **不参与**筛选（盈亏/年份/个股都不影响它）—— 它的 net/win 全是 null，
        跟随筛选就会凭空消失，而「明明有 57 笔没结算」正是要展示的东西。
     2. 必须**显式说明不计入统计**，否则用户会拿它去和「完成交易」相加，
        或以为胜率漏算了 —— 收益池里没有它们。
   三类 kind：
     · holding = 真·持有中（买了、还拿着，到期日越过数据边界）
     · nobuy   = T+1 买不进（一字涨停/停牌，信号作废，从未建仓）
     · stuck   = 买得进、持有期也走完了，但**卖出日无有效开盘价**（停牌/退市/数据缺口）
                 → 算不出收益，无法结算。既不是持有中，也不是买不进。 */
function tradeOpenBlock(r) {
  const rows = r.open_rows || [];
  if (!rows.length) return '';
  const nh = r.n_open_holding || 0, nb = r.n_open_nobuy || 0;
  const nk = r.n_open_stuck != null ? r.n_open_stuck
    : Math.max(0, rows.length - nh - nb);        // 向后兼容：旧响应没这个字段时反推
  const tbl = rows.map(it => `<tr class="openrow ${it.kind === 'nobuy' ? 'nb' : ''}">
    <td class="l">${it.code}</td>
    <td class="nm">${it.name}</td>
    <td class="l" style="color:#5c6270">${it.ind}</td>
    <td><span class="pill bd${it.board}">${BD(it.board)}</span></td>
    <td style="color:var(--tx3)">${it.signal_date || '—'}</td>
    <td>${it.entry_date || '—'}</td>
    <td>${F(it.buy, 2)}</td>
    <td class="l">${it.kind === 'holding'
      ? `<span class="pill om">持有中 ${it.hold_days}/${it.hold} 日</span>
         <span class="dimnote">还需 ${it.left_days} 日</span>`
      : it.kind === 'stuck'
      ? `<span class="pill nb">卖出日无价</span>
         <span class="dimnote">停牌 / 退市 / 数据缺口，算不出收益</span>`
      : `<span class="pill nb">T+1 买不进</span>
         <span class="dimnote">一字板 / 停牌，信号作废</span>`}</td>
    <td style="color:#5c6270">${it.size_grp == null ? '—' : 'M' + (it.size_grp + 1)}</td>
    <td><span class="pill ${it.d_px60 === 1 ? 'd1' : it.d_px60 === 2 ? 'd2' : 'dn'}">D${it.d_px60 ?? '—'}</span></td>
    <td style="color:var(--tx3)">—</td>
    <td style="color:var(--tx3)">—</td>
  </tr>`).join('');
  return `<div class="card" style="margin-top:14px;background:#fffdf8;border-color:#f0e2c0">
    <h3>未结束交易 <span class="tag">${NUM(rows.length)} 笔 · 不计入统计</span></h3>
    <div class="desc">这些头寸<b>没有结局</b>，因此<b>不进入</b>上面的完成交易数、胜率、
      盈亏比、逐年统计与收益分布 —— 纳入会让结果偏乐观。分三类：</div>
    <div class="cap" style="margin:0 0 10px">
      <span class="pill om">持有中 ${nh} 笔</span>
      信号已出、<b>T+1 已经买入</b>，但到期日落在数据末尾之外 ——
      实盘里就是<b>还拿在手上</b>。<br>
      <span class="pill nb">T+1 买不进 ${nb} 笔</span>
      信号日次日<b>一字涨停或停牌</b>（<code>can_buy_open=False</code>），
      开盘价买不到 —— 这笔<b>从未建仓</b>，信号作废。这是空仓等待的天然成本，
      回测里不算收益，也不该算作亏损。<br>
      <span class="pill nb">卖出日无价 ${nk} 笔</span>
      买进了、持有期也走完了，但<b>卖出日没有有效开盘价</b>（停牌 / 退市整理 /
      数据缺口）→ <b>算不出收益</b>，无法结算。既不是「还拿着」，也不是「没买进」。
    </div>
    <div class="tw" style="max-height:320px;overflow-y:auto"><table><thead><tr>
      <th style="text-align:left">代码</th><th style="text-align:left">名称</th>
      <th style="text-align:left">行业</th><th>板块</th>
      <th>信号日</th><th>买入日</th><th>买入价</th><th style="text-align:left">状态</th>
      <th>市值组</th><th>距MA60</th><th>净收益</th><th>盈亏</th>
    </tr></thead><tbody>${tbl}</tbody></table></div>
    <div class="cap" style="margin-top:9px">
      按信号日倒序。导出 CSV 时这些行也会带上（状态列标「持有中」/「买不进」/「卖出日无价」），
      避免「导出后怎么少了几笔」。
    </div></div>`;
}

function renderTradeRows(rows) {
  const tb = $('#tbody-trades'); if (!tb) return;
  tb.innerHTML = rows.map(it => `<tr class="${it.win ? '' : 'lossc'}">
    <td class="rk2">${it.seq}</td>
    <td class="l">${it.code}</td>
    <td class="nm">${it.name}</td>
    <td class="l" style="color:#5c6270">${it.ind}</td>
    <td><span class="pill bd${it.board}">${BD(it.board)}</span></td>
    <td style="color:var(--tx3)">${it.signal_date || '—'}</td>
    <td>${it.entry_date || '—'}</td>
    <td style="color:${it.w == null ? 'var(--tx3)' : '#a8622c'};font-family:var(--mono)">${it.w == null ? '—' : F(it.w, 1, '%')}</td>
    <td>${it.exit_date || '—'}</td>
    <td>${F(it.buy, 2)}</td>
    <td>${F(it.sell, 2)}</td>
    <td class="${cls(it.net)}" style="font-weight:600">${PCTN(it.net)}
      <span class="tkx ${it.win ? 'w' : 'l'}">${it.win ? '盈' : '亏'}</span></td>
    <td class="${cls(it.ret)}">${PCTN(it.ret)}</td>
    <td class="${cls(it.bench)}">${PCTN(it.bench)}</td>
    <td class="${cls(it.excess)}">${PCTN(it.excess)}</td>
    <!-- ⚠️ 市值组是分位序号（0 起 = 最小 10%），不是十分位档位编号；
         直接展示成 M1~M10 以免与下方的「距MA60 D1~D10」混淆 -->
    <td style="color:#5c6270">${it.size_grp == null ? '—' : 'M' + (it.size_grp + 1)}</td>
    <td><span class="pill ${it.d_px60 === 1 ? 'd1' : it.d_px60 === 2 ? 'd2' : 'dn'}">D${it.d_px60 ?? '—'}</span></td>
    <td style="color:#5c6270">${MONEY(it.fin_rev)}</td>
    <td class="${cls(it.fin_rev_yoy)}">${F(it.fin_rev_yoy, 1, '%')}</td>
    <td style="color:#5c6270">${MONEY(it.fin_np)}</td>
    <td class="${cls(it.fin_np_yoy)}">${F(it.fin_np_yoy, 1, '%')}</td>
  </tr>`).join('');
}

function drawTradeCharts(r) {
  // ---- 收益分布直方图
  const e = r.hist.edges, c = r.hist.counts;
  const labels = c.map((_, i) => {
    const a = e[i], b = e[i + 1];
    return (a >= 0 && b <= 0) ? '0' : `${a}~${b}`;
  });
  const hc = mkChart('ch-hist');
  hc && hc.setOption({
    grid: { left: 56, right: 16, top: 24, bottom: 34 },
    tooltip: { trigger: 'axis', ...TIP,
      formatter: p => `<b>${p[0].name}%</b><br>笔数: <b>${NUM(p[0].value)}</b>` },
    xAxis: { type: 'category', data: labels, ...AX,
      axisLabel: { ...AX.axisLabel, rotate: 38, fontSize: 9.5 },
      name: '净收益率 (%)', nameLocation: 'middle', nameGap: 30,
      nameTextStyle: { fontSize: 10.5, color: '#8d94a3' } },
    yAxis: { type: 'value', ...AX,
      axisLabel: { ...AX.axisLabel,
        formatter: v => v >= 1000 ? (v / 1000).toFixed(0) + 'k' : v } },
    series: [{
      type: 'bar', barMaxWidth: 30,
      data: c.map((v, i) => {
        const mid = (e[i] + e[i + 1]) / 2;
        return { value: v, itemStyle: { color: mid >= 0 ? '#2ea06a' : '#cf5b5b',
          borderRadius: mid >= 0 ? [3, 3, 0, 0] : [0, 0, 3, 3] } };
      }),
    }]
  });

  // ---- 逐年图
  const Y = r.yearly || [];
  const yc = mkChart('ch-tyear');
  yc && yc.setOption({
    grid: { left: 50, right: 50, top: 34, bottom: 26 },
    tooltip: { trigger: 'axis', ...TIP,
      formatter: p => { const i = p[0].dataIndex, y = Y[i];
        return `<b>${y.year} 年</b><br>交易 ${NUM(y.n)} 笔 · 盈利 ${NUM(y.n_win)} 笔<br>` +
          `平均净收益: <b>${PCT(y.mean, 2)}</b><br>胜率: <b>${PCT(y.win, 1)}</b><br>` +
          `中位数: <b>${PCT(y.median, 2)}</b><br>最佳 ${PCT(y.best, 1)} / 最差 ${PCT(y.worst, 1)}`; } },
    legend: { top: 2, right: 6, itemWidth: 12, itemHeight: 8,
      textStyle: { fontSize: 11, color: '#5c6270' } },
    xAxis: { type: 'category', data: Y.map(x => String(x.year)), ...AX },
    yAxis: [
      { type: 'value', ...AX, axisLabel: { ...AX.axisLabel, formatter: v => v + '%' } },
      { type: 'value', max: 100, ...AX, splitLine: { show: false },
        axisLabel: { ...AX.axisLabel, formatter: v => v + '%' } },
    ],
    series: [
      { name: '平均净收益', type: 'bar', barMaxWidth: 34, yAxisIndex: 0,
        data: Y.map(x => ({ value: +(x.mean * 100).toFixed(2),
          itemStyle: { color: x.mean >= 0 ? '#2ea06a' : '#cf5b5b',
            borderRadius: x.mean >= 0 ? [3, 3, 0, 0] : [0, 0, 3, 3] } })) },
      { name: '胜率', type: 'line', yAxisIndex: 1, smooth: true, symbolSize: 6,
        data: Y.map(x => +(x.win * 100).toFixed(1)),
        lineStyle: { width: 1.6, color: '#c98a2b' }, itemStyle: { color: '#c98a2b' } },
    ]
  });
}

function bindTradeControls(r) {
  // 表头排序
  $$('#p-trades th.ts').forEach(th => th.addEventListener('click', () => {
    const k = th.dataset.k;
    if (TST.sort === k) TST.order = TST.order === 'desc' ? 'asc' : 'desc';
    else { TST.sort = k; TST.order = (k === 'code' || k === 'name' || k === 'ind') ? 'asc' : 'desc'; }
    TST.page = 1; runTrades();
  }));
  // 盈亏筛选
  $$('#t-win button').forEach(b => b.addEventListener('click', () => {
    const v = b.dataset.w;
    TST.win = v === '' ? null : (v === '1');
    TST.page = 1; runTrades();
  }));
  // 应用筛选
  const apply = () => {
    const y = $('#t-year') ? $('#t-year').value : '';
    TST.year = y ? parseInt(y, 10) : null;
    TST.code = ($('#t-code') ? $('#t-code').value.trim() : '');
    const ps = $('#t-size') ? parseInt($('#t-size').value, 10) : 50;
    if (ps !== TST.page_size) { TST.page_size = ps; TST.page = 1; }
    runTrades();
  };
  const ap = $('#t-apply'); ap && ap.addEventListener('click', apply);
  const sz = $('#t-size'); sz && sz.addEventListener('change', apply);
  const cd = $('#t-code'); cd && cd.addEventListener('keydown', e => {
    if (e.key === 'Enter') apply();
  });
  const rs = $('#t-reset'); rs && rs.addEventListener('click', () => {
    TST.win = null; TST.year = null; TST.code = ''; TST.page = 1;
    runTrades();
  });
  // 分页
  const pgn = $('#t-pg');
  if (pgn) {
    const tot = r.n_page, cur = r.page;
    const mk = (label, pg, dis, on) =>
      `<button ${dis ? 'disabled' : ''} class="${on ? 'on' : ''}" data-pg="${pg}">${label}</button>`;
    let btns = mk('首页', 1, cur === 1, false);
    btns += mk('‹', Math.max(1, cur - 1), cur === 1, false);
    let a = Math.max(1, cur - 2), b = Math.min(tot, a + 4);
    a = Math.max(1, b - 4);
    for (let i = a; i <= b; i++) btns += mk(String(i), i, false, i === cur);
    btns += mk('›', Math.min(tot, cur + 1), cur === tot, false);
    btns += mk('末页', tot, cur === tot, false);
    pgn.innerHTML = btns;
    $$('#t-pg button').forEach(x => x.addEventListener('click', () => {
      TST.page = parseInt(x.dataset.pg, 10); runTrades();
    }));
  }
  // 导出
  // ⚠️ 必须带上仓位约束参数（collect() 已含），否则导出的 CSV
  //    与页面上看到的「实际建仓」明细不一致（导成了不限仓位口径）。
  const ex = $('#t-exp'); ex && ex.addEventListener('click', async () => {
    const body = { kind: 'trades', params: collect(), pool: currentPool() };
    if (TST.win !== null) body.win = TST.win;
    if (TST.year) body.year = TST.year;
    const resp = await fetch('/api/export', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    if (!resp.ok) { alert('导出失败：' + resp.status); return; }
    const blob = await resp.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `超跌反转交易明细_H${r.hold}` +
      (r.plan ? `_持${r.plan.max_pos}只日${r.plan.max_new}只` : '') + '.csv';
    document.body.appendChild(a); a.click(); a.remove();
  });
}

/* ---------------- 实盘持仓盈亏 ----------------
   口径（与后端 /api/holdings 一致，改前先对齐）
   · 现价 = **未复权**收盘价 —— 用户填的成本价是券商里的实际成交价，只有未复权价
     和它同尺度；用复权价比，遇到分红送转就会凭空多出/少掉一截盈亏。
   · 浮动盈亏 =（现价 − 成本价）× 股数；今日盈亏 =（现价 − 上一有效收盘）× 股数。
   · 盈亏**不落盘**，每次进页面/点刷新都按最新行情重算（后端现算，前端只负责展示）。
   · 一条记录 = 一个**买入批次**，同一只票可以有多条（各自成本与买入日不同）。
   ⚠️ 红涨绿跌：盈利用 .pos（红），亏损用 .neg（绿）—— A 股习惯，别按欧美反过来。 */
let HOLD = null;          // 最近一次 /api/holdings 响应（**手动账本**）
let HEDIT = null;         // 正在编辑的批次 id（null = 新增）
const HST = { k: 'pnl_pct', asc: false };   // 明细表排序（默认浮动盈亏率降序）
/* 策略持仓（回测自动）：**不用录入** —— 它就是回测里「已经买了、持有期还没走完」的头寸。
   与手动账本的分工见下方 posBlockHTML() 的注释。 */
let POS = null;           // 最近一次 /api/positions 响应
let POSERR = '';          // 取策略持仓失败的原因（不影响下面的手动账本）
const PST = { k: 'exit_est', asc: true };   // 策略表排序（默认最快到期在前）
/* 金额：带千分位与两位小数。不用 MONEY() —— 那个按 万/亿 缩写，个人账本要看准数。
   ⚠️ 负号要放**货币符号前面**（-¥1,234.00），别写成 ¥-1,234.00 —— 后者是
      账目界面里最刺眼的一种格式。四舍五入后为 0 的极小亏损按 ¥0.00 处理。 */
const hYuan = v => {
  if (v === null || v === undefined || !isFinite(v)) return '—';
  const a = Math.abs(v);
  const s = a.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return (v < 0 && a >= 0.005 ? '-¥' : '¥') + s;
};
/* 股数：最多两位小数（基金/ETF 可能不是整百），别用 NUM() 四舍五入到整数 */
const hQty = v => (v === null || v === undefined || !isFinite(v)) ? '—'
  : v.toLocaleString('zh-CN', { maximumFractionDigits: 2 });

async function runHoldings() {
  const el = $('#p-hold');
  if (!el) return;
  el.innerHTML = `<div class="card"><div class="empty">
    <span class="spin"></span> 正在加载持仓与最新收盘价…</div></div>`;
  // 两块并行：策略持仓（POST 带当前参数，~1s）+ 手动账本（GET，~5ms）
  // ⚠️ 策略持仓失败**只降级自己**，不能把手动账本一起拖崩 —— 反过来也一样。
  const pReq = (() => {
    let body = {};
    try { body = { params: collect(), pool: currentPool() }; } catch (e) { /* 用后端默认参数 */ }
    return api('/api/positions', body)
      .then(x => ({ ok: !!(x && x.ok), data: x, err: (x && x.error) || '' }))
      .catch(e => ({ ok: false, data: null, err: e.message || String(e) }));
  })();
  let r;
  try {
    r = await api('/api/holdings', null, true);
  } catch (e) {
    el.innerHTML = `<div class="errbox"><b>持仓盈亏加载失败：</b>${e.message}</div>`;
    return;
  }
  const pr = await pReq;
  POS = pr.ok ? pr.data : null;
  POSERR = pr.ok ? '' : pr.err;
  HOLD = r;
  renderHoldings(r);
}

function hSorted(rows) {
  const k = HST.k, s = HST.asc ? 1 : -1;
  return rows.slice().sort((a, b) => {
    // 无行情的批次永远沉底（不管升序还是降序）—— 它们没有可比的价格
    if (!!a.noquote !== !!b.noquote) return a.noquote ? 1 : -1;
    if (k === 'code' || k === 'name' || k === 'buy_date' || k === 'price_date') {
      return String(a[k] || '').localeCompare(String(b[k] || ''), 'zh-CN') * s;
    }
    const ok = v => (v === null || v === undefined || !isFinite(v)) ? -Infinity : v;
    return (ok(a[k]) - ok(b[k])) * s;
  });
}

/* ---------------- 策略持仓（回测自动） ----------------
   这就是「当前持仓」：回测里已经买入、持有期还没走完的头寸。**不需要手工录入。**
   和下面手动账本的区别（⚠️ 两块合计不能相加）：
     · 本块：策略让你现在拿着什么 —— 随左侧参数变化，只有**收益率**没有金额
       （回测不知道你投了多少钱，硬编股数就是编数据）
     · 手动账本：你真的买了什么 —— 有成本/股数，能算金额盈亏
     · 价格口径也不同：本块用**复权**价（与回测买入价同尺度），
       账本用**未复权**价（与券商成交价同尺度）。 */
function pTh(k, label, title) {
  const on = PST.k === k;
  const ar = on ? `<span class="ar">${PST.asc ? '▲' : '▼'}</span>` : '';
  return `<th class="pts" data-k="${k}"${title ? ` title="${title}"` : ''}>${label}${ar}</th>`;
}

function pSorted(rows) {
  const k = PST.k, s = PST.asc ? 1 : -1;
  return rows.slice().sort((a, b) => {
    if (k === 'code' || k === 'name' || k === 'entry_date' || k === 'exit_est') {
      return String(a[k] || '').localeCompare(String(b[k] || ''), 'zh-CN') * s;
    }
    const okv = v => (v === null || v === undefined || !isFinite(v)) ? -Infinity : v;
    return (okv(a[k]) - okv(b[k])) * s;
  });
}

/* 持有期进度：已持有 / (已持有 + 还需)。left=0 表示下个交易日开盘就卖 → 标琥珀色 */
function pBar(h, l) {
  const tot = (h || 0) + (l || 0);
  const w = tot > 0 ? Math.min(100, (h / tot) * 100) : 100;
  return `<div class="pbar${l === 0 ? ' due' : ''}"
    title="已持有 ${h} / 共 ${tot} 个交易日"><i style="width:${w.toFixed(1)}%"></i></div>`;
}

function posBlockHTML() {
  if (POSERR) {
    return `<div class="errbox" style="margin-bottom:14px">
      <b>策略持仓没能取到：</b>${POSERR}
      <br>下面的手动账本不受影响。</div>`;
  }
  const p = POS;
  if (!p) return '';
  const rows = p.rows || [], t = p.totals;
  if (!rows.length) {
    return `<div class="card">
      <h3>策略持仓 <span class="tag">回测自动 · 持有期未走完</span></h3>
      <div class="empty">${p.msg || '当前参数下没有「持有中」的头寸 —— '
        + '最新交易日附近没出信号，或信号对应的持有期都已走完。'}</div>
    </div>`;
  }
  const winRate = (t.n_win + t.n_loss) > 0
    ? t.n_win / (t.n_win + t.n_loss) : null;
  let h = `<div class="btgrid" style="margin-bottom:12px">
    <div class="mc hl"><div class="l">持有中</div>
      <div class="v sm">${NUM(t.n_hold)} 笔</div>
      <div class="n">${NUM(t.n_stock)} 只股票${
        t.n_pending ? ` · ${NUM(t.n_pending)} 笔待买入` : ''}</div></div>
    <div class="mc"><div class="l">平均浮动收益率</div>
      <div class="v sm ${cls(t.avg_pct)}">${t.avg_pct == null ? '—' : PCT(t.avg_pct, 2)}</div>
      <div class="n">等权，不含成本</div></div>
    <div class="mc"><div class="l">中位数</div>
      <div class="v sm ${cls(t.med_pct)}">${t.med_pct == null ? '—' : PCT(t.med_pct, 2)}</div>
      <div class="n">比均值更抗极值</div></div>
    <div class="mc"><div class="l">赚钱占比</div>
      <div class="v sm">${winRate == null ? '—' : PCT(winRate, 1)}</div>
      <div class="n"><span class="pos">${NUM(t.n_win)}</span> 盈 /
        <span class="neg">${NUM(t.n_loss)}</span> 亏</div></div>
    <div class="mc"><div class="l">最快到期</div>
      <div class="v sm" style="font-size:13px">${t.next_exit || '—'}</div>
      <div class="n">之后陆续到期</div></div>
    <div class="mc"><div class="l">行情日</div>
      <div class="v sm" style="font-size:13px">${p.at || '—'}</div>
      <div class="n">持有期 H = ${NUM(p.hold)} 个交易日${
        t.capped ? ' · 已开仓位约束' : ''}</div></div>
  </div>`;

  const body = pSorted(rows).map(it => {
    const due = (it.left_days === 0);
    return `<tr${it.pending ? ' class="hq-no"' : ''}>
      <td class="l">${it.code}</td>
      <td class="nm">${it.name || '—'}</td>
      <td>${it.entry_date || '待买入'}</td>
      <td>${NUM(it.hold_days)}</td>
      <td>${NUM(it.left_days)}</td>
      <td class="${due ? 'xdue' : ''}">${it.exit_est || '—'}</td>
      <td>${F(it.buy, 2)}</td>
      <td>${F(it.price, 2)}</td>
      <td class="${cls(it.pnl_pct)}" style="font-weight:600">${
        it.pnl_pct == null ? '—' : PCT(it.pnl_pct, 2)}</td>
      <td class="${cls(it.day_chg)}">${it.day_chg == null ? '—' : PCT(it.day_chg, 2)}</td>
      <td style="width:70px">${pBar(it.hold_days, it.left_days)}</td>
    </tr>`;
  }).join('');

  h += `<div class="card">
    <h3>策略持仓 <span class="tag">回测自动 · 无需录入</span>
      <span style="flex:1"></span>
      <button class="btn2" id="p-refresh">按当前参数重算</button></h3>
    <div class="desc">这些是回测里「已经买入、持有期还没走完」的头寸 —— 就是你现在的持仓，
      <b>不用手工录</b>。改了左侧参数（尤其持有期 H、仓位约束）点「按当前参数重算」。</div>
    <div class="tw" style="max-height:58vh;overflow-y:auto">
      <table id="p-tbl">
        <thead><tr>
          ${pTh('code', '代码')}
          ${pTh('name', '名称')}
          ${pTh('entry_date', '买入日', 'T+1 开盘买入的交易日')}
          ${pTh('hold_days', '已持有', '已持有的交易日数（含买入当天）')}
          ${pTh('left_days', '还需', '还需持有几个交易日；0 = 下个交易日开盘卖出')}
          ${pTh('exit_est', '预计卖出日', '按工作日推算，未扣法定节假日 —— 精确值看「还需」列')}
          ${pTh('buy', '买入价', 'T+1 开盘价（复权），与回测同源')}
          ${pTh('price', '现价', '最新复权收盘价 —— 与买入价同尺度')}
          ${pTh('pnl_pct', '浮动收益率', '现价/买入价 − 1，纯价格、不含交易成本')}
          ${pTh('day_chg', '今日涨跌', '现价 / 上一有效收盘 − 1')}
          <th>持有进度</th>
        </tr></thead>
        <tbody>${body}</tbody>
      </table>
    </div>
    <div class="cap">口径：买入价 = T+1 开盘、持有 H 个交易日后 T+1+H 开盘卖出；
      价格用<b>复权</b>价（与回测同尺度），浮动收益率<b>不含</b>佣金/印花税。
      <b>没有金额</b>——回测不知道你实际投了多少钱，硬算股数就是编数据。
      预计卖出日按<b>工作日</b>推算，法定节假日未扣除（「还需 N 个交易日」是精确值）。</div>
  </div>`;
  return h;
}

function hTh(k, label, title) {
  const on = HST.k === k;
  const ar = on ? `<span class="ar">${HST.asc ? '▲' : '▼'}</span>` : '';
  return `<th class="ts" data-k="${k}"${title ? ` title="${title}"` : ''}>${label}${ar}</th>`;
}

function hBtns(it) {
  return `<span style="display:inline-flex;gap:5px">
    <button class="btn2" data-h-edit="${it.id}" style="padding:2px 8px;font-size:11px">编辑</button>
    <button class="btn2" data-h-del="${it.id}" style="padding:2px 8px;font-size:11px">删除</button>
  </span>`;
}

function hFormHTML() {
  return `<div class="card">
    <h3>${HEDIT ? '修改持仓批次' : '录入持仓'} <span class="tag">一条 = 一个买入批次</span></h3>
    <div class="desc">成本价填<b>券商里的实际成交价</b>（未复权）。同一只票可以分多批录入，
      每批各自记成本与买入日，盈亏分别算、合计再汇总。代码填 6 位数字即可。</div>
    <div class="hform">
      <label>代码<input id="h-code" maxlength="12" placeholder="600000"></label>
      <label>成本价<input id="h-cost" type="number" step="0.001" min="0" placeholder="8.50"></label>
      <label>股数<input id="h-shares" type="number" step="1" min="0" placeholder="1000"></label>
      <label>买入日<input id="h-date" type="date"></label>
      <label>备注<input id="h-note" maxlength="60" placeholder="可选"></label>
      <div class="acts">
        <button class="btn2" id="h-save">${HEDIT ? '保存修改' : '添加持仓'}</button>
        ${HEDIT ? '<button class="btn2" id="h-cancel">取消</button>' : ''}
      </div>
    </div>
    <div class="cap" id="h-msg" style="margin-top:9px"></div>
  </div>`;
}

function renderHoldings(r) {
  const el = $('#p-hold');
  if (!el) return;
  const rows = r.rows || [], t = r.totals || {};
  let h = posBlockHTML();          // ← 策略持仓置顶（回测自动，**不需要录入**）

  // 手动账本收进折叠区：只想看「现在拿着什么」的人，不必先撞上一张录入表单。
  const hasLots = rows.length > 0;
  h += `<details class="hfold"${hasLots ? ' open' : ''}>
    <summary>我的实盘持仓（手动录入）<span class="cnt">${hasLots
      ? NUM(t.n_lot) + ' 笔 · 浮动盈亏 ' + hYuan(t.pnl)
      : '未录入 · 想按真实成交价对账再点开'}</span></summary>
    <div class="foldbody">`;

  // ---- 汇总卡片（没有持仓时不渲染一堆 0，免得像「数据坏了」）
  if (hasLots) {
    h += `<div class="mgrid" style="margin-bottom:14px">
      <div class="mc hl"><div class="l">浮动盈亏</div>
        <div class="v ${cls(t.pnl)}">${hYuan(t.pnl)}</div>
        <div class="n">${t.pnl_pct == null ? '—' : PCT(t.pnl_pct, 2)}</div></div>
      <div class="mc"><div class="l">持仓市值</div>
        <div class="v sm">${hYuan(t.mv)}</div>
        <div class="n">总成本 ${hYuan(t.cost)}</div></div>
      <div class="mc"><div class="l">今日盈亏</div>
        <div class="v sm ${cls(t.day_pnl)}">${hYuan(t.day_pnl)}</div>
        <div class="n">${t.day_pct == null ? '—' : PCT(t.day_pct, 2)}</div></div>
      <div class="mc"><div class="l">持仓笔数</div>
        <div class="v sm">${NUM(t.n_lot)} 笔</div>
        <div class="n">${NUM(t.n_stock)} 只股票${
          t.n_lot > t.n_stock ? '（有分批）' : ''}</div></div>
      <div class="mc"><div class="l">盈利 / 亏损</div>
        <div class="v sm"><span class="pos">${NUM(t.n_win)}</span>
          <span style="color:var(--tx3)">/</span>
          <span class="neg">${NUM(t.n_loss)}</span></div>
        <div class="n">按批次计</div></div>
      <div class="mc"><div class="l">行情日</div>
        <div class="v sm" style="font-size:13px">${r.at || '—'}</div>
        <div class="n">${r.n_noquote ? `${r.n_noquote} 笔无行情` : '全部有行情'}</div></div>
    </div>`;
  }

  h += hFormHTML();

  if (r.n_noquote) {
    h += `<div class="warnbox"><b>有 ${r.n_noquote} 笔查不到行情</b>（${
      (r.noquote || []).join('、')}）—— 可能是代码打错，或该股已退市 / 长期停牌
      被剔出面板。这些批次<b>不计入</b>上面的合计，请核对后修正或删除。</div>`;
  }

  if (!hasLots) {
    h += `<div class="card"><div class="empty">
      还没有录入持仓。<br>用上面的表单加第一笔，盈亏会按最新收盘价自动算。</div></div>
      </div></details>`;
    el.innerHTML = h;
    bindHold();
    return;
  }

  // ---- 明细表（含合计行）
  const priced = rows.filter(x => !x.noquote);
  const sumSh = priced.reduce((a, b) => a + (b.shares || 0), 0);
  const body = hSorted(rows).map(it => {
    if (it.noquote) {
      return `<tr class="hq-no">
        <td class="l">${it.code}</td>
        <td class="nm">${it.name || '—'}</td>
        <td>${it.buy_date || '—'}</td>
        <td>—</td>
        <td>${F(it.cost, 3)}</td>
        <td>—</td>
        <td>—</td>
        <td>—</td>
        <td>${hQty(it.shares)}</td>
        <td>${hYuan(it.cost_amt)}</td>
        <td>—</td>
        <td>—</td>
        <td>—</td>
        <td class="l">${hBtns(it)}</td>
      </tr>`;
    }
    const stale = (it.stale_days > 0)
      ? `<span class="stale" title="该股已有 ${it.stale_days} 个交易日没有行情（停牌 / 退市整理），现价不是最新">⚠</span>`
      : '';
    return `<tr>
      <td class="l">${it.code}</td>
      <td class="nm">${it.name || '—'}</td>
      <td>${it.buy_date || '—'}</td>
      <td>${it.hold_days == null ? '—' : NUM(it.hold_days)}</td>
      <td>${F(it.cost, 3)}</td>
      <td>${F(it.price, 2)}${stale}</td>
      <td style="color:var(--tx3)">${it.price_date || '—'}</td>
      <td class="${cls(it.chg)}">${it.chg == null ? '—' : PCT(it.chg, 2)}</td>
      <td>${hQty(it.shares)}</td>
      <td>${hYuan(it.cost_amt)}</td>
      <td>${hYuan(it.mv)}</td>
      <td class="${cls(it.pnl)}" style="font-weight:600">${hYuan(it.pnl)}</td>
      <td class="${cls(it.pnl_pct)}" style="font-weight:600">${
        it.pnl_pct == null ? '—' : PCT(it.pnl_pct, 2)}</td>
      <td class="l">${hBtns(it)}</td>
    </tr>`;
  }).join('');

  h += `<div class="card">
    <h3>持仓明细 <span class="tag">盈亏按 ${r.at || '最新'} 收盘价现算</span>
      <span style="flex:1"></span>
      <button class="btn2" id="h-refresh">刷新行情</button></h3>
    <div class="tw" style="max-height:60vh;overflow-y:auto">
      <table id="h-tbl">
        <thead><tr>
          ${hTh('code', '代码')}
          ${hTh('name', '名称')}
          ${hTh('buy_date', '买入日')}
          ${hTh('hold_days', '持有(天)', '自然日：买入日 → 该股最新行情日')}
          ${hTh('cost', '成本价')}
          ${hTh('price', '现价', '未复权收盘价，与券商成交价同尺度')}
          ${hTh('price_date', '行情日', '该股最后一条行情的日期；停牌票会早于面板末日')}
          ${hTh('chg', '今日涨跌', '现价 / 上一有效收盘 − 1')}
          ${hTh('shares', '股数')}
          ${hTh('cost_amt', '成本额')}
          ${hTh('mv', '市值')}
          ${hTh('pnl', '浮动盈亏')}
          ${hTh('pnl_pct', '盈亏率')}
          <th>操作</th>
        </tr></thead>
        <tbody>${body}</tbody>
        <tfoot><tr style="background:#fafbfc;font-weight:600">
          <td class="l">合计</td><td></td><td></td><td></td>
          <td>${sumSh > 0 ? F(t.cost / sumSh, 3) : '—'}</td>
          <td>—</td><td></td>
          <td class="${cls(t.day_pct)}">${t.day_pct == null ? '—' : PCT(t.day_pct, 2)}</td>
          <td>${hQty(sumSh)}</td>
          <td>${hYuan(t.cost)}</td>
          <td>${hYuan(t.mv)}</td>
          <td class="${cls(t.pnl)}">${hYuan(t.pnl)}</td>
          <td class="${cls(t.pnl_pct)}">${t.pnl_pct == null ? '—' : PCT(t.pnl_pct, 2)}</td>
          <td></td>
        </tr></tfoot>
      </table>
    </div>
    <div class="cap">口径：现价 = <b>未复权</b>收盘价；浮动盈亏 =（现价 − 成本价）× 股数
      （<b>不含</b>佣金/印花税，也不含已派现金分红）；持有天数为<b>自然日</b>。
      盈亏不落盘，每次进页面按最新行情重算，账本里只存你填的代码/成本/股数/买入日
      （<code>data/holdings.json</code>）。</div>
  </div>
  </div></details>`;

  el.innerHTML = h;
  bindHold();
}

function bindHold() {
  // 策略持仓表头排序（用 .pts，与手动账本的 .ts 分开，互不干扰）
  $$('#p-hold th.pts').forEach(th => th.addEventListener('click', () => {
    const k = th.dataset.k;
    if (PST.k === k) PST.asc = !PST.asc;
    else PST.asc = (k === 'code' || k === 'name' || k === 'entry_date'
                    || k === 'exit_est');
    PST.k = k;
    if (HOLD) renderHoldings(HOLD);
  }));
  const prf = $('#p-refresh');
  prf && prf.addEventListener('click', runHoldings);   // 按当前参数重算（含最新行情）
  // 表头排序：纯前端重排，不再打接口
  $$('#p-hold th.ts').forEach(th => th.addEventListener('click', () => {
    const k = th.dataset.k;
    if (HST.k === k) HST.asc = !HST.asc;
    else HST.asc = (k === 'code' || k === 'name' || k === 'buy_date'
                    || k === 'price_date');
    HST.k = k;
    if (HOLD) renderHoldings(HOLD);
  }));
  const rf = $('#h-refresh'); rf && rf.addEventListener('click', runHoldings);
  const sv = $('#h-save'); sv && sv.addEventListener('click', hSave);
  const cc = $('#h-cancel'); cc && cc.addEventListener('click', () => {
    HEDIT = null;
    if (HOLD) renderHoldings(HOLD);
  });
  // 编辑：先重绘（按钮文案切到「保存修改」），再回填表单
  $$('#p-hold [data-h-edit]').forEach(b => b.addEventListener('click', () => {
    const it = (HOLD && HOLD.rows || []).find(x => x.id === b.dataset.hEdit);
    if (!it) return;
    HEDIT = it.id;
    renderHoldings(HOLD);
    const set = (sel, v) => { const e = $(sel); if (e) e.value = (v == null ? '' : v); };
    set('#h-code', String(it.code || '').split('.')[0]);
    set('#h-cost', it.cost);
    set('#h-shares', it.shares);
    set('#h-date', it.buy_date || '');
    set('#h-note', it.note || '');
    const c = $('#h-code');
    if (c) { c.focus(); const card = c.closest('.card'); card && card.scrollIntoView({ block: 'nearest' }); }
  }));
  // 删除
  $$('#p-hold [data-h-del]').forEach(b => b.addEventListener('click', async () => {
    const it = (HOLD && HOLD.rows || []).find(x => x.id === b.dataset.hDel);
    const nm = it ? `${it.name || it.code}（${it.cost} × ${hQty(it.shares)} 股）` : b.dataset.hDel;
    if (!confirm(`确定删除这笔持仓？\n${nm}`)) return;
    try {
      const r = await api('/api/holdings/delete', { id: b.dataset.hDel });
      if (!r.ok) { alert(r.msg || '删除失败'); return; }
      if (HEDIT === b.dataset.hDel) HEDIT = null;
      await runHoldings();
    } catch (e) { alert('删除失败：' + e.message); }
  }));
  // 回车即提交（备注框也一样，省得去点按钮）
  $$('#p-hold .hform input').forEach(i => i.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); hSave(); }
  }));
}

async function hSave() {
  const say = (msg, bad) => {
    const el = $('#h-msg');
    if (!el) return;
    el.textContent = msg || '';
    el.style.color = bad ? '#c0392b' : '#0d7a3a';
  };
  const val = sel => { const e = $(sel); return e ? e.value : ''; };
  const code = String(val('#h-code') || '').trim();
  const cost = parseFloat(val('#h-cost'));
  const shares = parseFloat(val('#h-shares'));
  if (!code) return say('请填写股票代码（6 位数字，如 600000）', true);
  if (!(cost > 0)) return say('成本价要填大于 0 的数字', true);
  if (!(shares > 0)) return say('股数要填大于 0 的数字', true);

  const body = { code, cost, shares,
                 buy_date: val('#h-date') || '', note: val('#h-note') || '' };
  const editing = !!HEDIT;
  if (editing) body.id = HEDIT;
  const btn = $('#h-save');
  if (btn) { btn.disabled = true; btn.textContent = '保存中…'; }
  try {
    const r = await api('/api/holdings/save', body);
    if (!r.ok) {
      say(r.msg || '保存失败', true);
      if (btn) { btn.disabled = false; btn.textContent = editing ? '保存修改' : '添加持仓'; }
      return;
    }
    HEDIT = null;
    await runHoldings();                  // 重绘：表单清空、合计更新
    say(r.msg || '已保存', false);
  } catch (e) {
    say('保存失败：' + e.message, true);
    if (btn) { btn.disabled = false; btn.textContent = editing ? '保存修改' : '添加持仓'; }
  }
}

/* ---------------- 参数寻优 ---------------- */
let TUNE = null;                                  // 最近一次寻优响应
let TUNE_SORT = { k: null, asc: false };          // 当前表格排序（表头点击可换）

$('#tune_run').addEventListener('click', async () => {
  const axes = {}; $$('#axes input').forEach(i => { if (i.checked) axes[i.value] = true; });
  if (!Object.keys(axes).length) { alert('请至少勾选一个待寻优维度'); return; }
  const b = $('#tune_run'); b.innerHTML = '<span class="spin"></span> 寻优中'; b.disabled = true;
  $('#tune-out').innerHTML = `<div class="card"><div class="empty">
    <span class="spin"></span> 正在遍历参数组合并逐一回测…</div></div>`;
  try {
    const r = await api('/api/tune', { params: collect(), pool: currentPool(), axes });
    renderTune(r);
  } catch (e) {
    $('#tune-out').innerHTML = `<div class="errbox"><b>寻优失败：</b>${e.message}</div>`;
    setTuneStat('寻优失败：' + e.message, 'bad');
  } finally { b.textContent = '开始寻优'; b.disabled = false; }
});

/* 「应用最优」= 应用当前排序下的第 1 行。
   ⚠️ 用 tuneSorted() 而不是 rows[0]：表格可能被表头点击换过排序，
      用户看到的第 1 行才是他要的「最优」。 */
$('#tune_apply_top').addEventListener('click', () => {
  if (!TUNE || !(TUNE.rows || []).length) { setTuneStat('先跑一次寻优', 'bad'); return; }
  applyTuneRow(0);
});

/* 寻优相关的状态提示（失败 / 已应用），与「预设方案」的 setPresetStat 分开，
   避免两处互相覆盖。 */
function setTuneStat(msg, kind) {
  const el = $('#tune_stat');
  if (!el) return;
  el.textContent = msg || '';
  el.style.color = kind === 'bad' ? '#c0392b' : (msg ? '#0d7a3a' : '');
}

/* 勾了节奏轴 / 选股规则轴 → 顶部立刻提示「本页会切到容量口径」，
   别等结果出来才发现排序依据变了（表头一换容易懵）。
   ⚠️ 这两个轴必须一起判断：`pick` 同样是容量层参数，单独勾它也会进容量口径。 */
const TUNE_CAP_AXES = ['rhythm', 'pick'];
const _axesBox = $('#axes');
if (_axesBox) _axesBox.addEventListener('change', () => {
  const on = $$('#axes input').some(i => TUNE_CAP_AXES.includes(i.value) && i.checked);
  const hint = $('#tune_cap_hint');
  if (hint) hint.style.display = on ? '' : 'none';
});

/* 寻优表的列定义。
   ⚠️ 两种口径的列**不混排** —— 把「不限仓位的胜率/PF」和「账户资金口径的 CAGR」
      放进同一行，会让人以为它们同源。容量模式下整表换成容量列。
   `asc0:1` = 越小越好（首次点击按升序），其余默认降序。 */
function tuneCols(cap) {
  const neg = () => 'neg';
  const nneg = (v, x) => (x.n_neg === 0 ? 'pos' : x.n_neg <= 2 ? 'neu' : 'neg');
  const nnf = (v, x) => `${x.n_neg}/${x.n_year}`;
  return cap ? [
    { k: 'cap_cagr',   t: '账户CAGR',   c: v => cls(v), f: v => PCT(v) },
    { k: 'cap_sharpe', t: '账户Sharpe', b: 1,          f: v => F(v, 2) },
    { k: 'cap_mdd',    t: '账户MDD',    c: neg, asc0: 1, f: v => PCT(v) },
    { k: 'inv_cagr',   t: '已投CAGR',   c: v => cls(v), f: v => PCT(v) },
    { k: 'avg_pos',    t: '平均持仓',   f: v => F(v, 1, ' 只') },
    { k: 'fill_pct',   t: '建仓率',     f: v => PCT(v, 1) },
    { k: 'n_signal',   t: '信号数',     f: v => NUM(v) },
    { k: 'n_neg',      t: '负年份',     c: nneg, asc0: 1, f: nnf },
    { k: 'is_',        t: 'IS',         c: v => cls(v), f: v => PCT(v) },
    { k: 'oos',        t: 'OOS',        c: v => cls(v), f: v => PCT(v) },
  ] : [
    { k: 'cagr',       t: 'CAGR',       c: v => cls(v), f: v => PCT(v) },
    { k: 'mdd',        t: 'MDD',        c: neg, asc0: 1, f: v => PCT(v) },
    { k: 'sharpe',     t: 'Sharpe',     b: 1,          f: v => F(v, 2) },
    { k: 'win',        t: '胜率',       f: v => PCT(v) },
    { k: 'pf',         t: 'PF',         f: v => F(v, 2) },
    { k: 'exw20',      t: 'exw20',      c: v => cls(v), f: v => F(v * 100, 3, '%') },
    { k: 't20',        t: 't20',        f: v => F(v, 2) },
    { k: 'n_signal',   t: '信号数',     f: v => NUM(v) },
    { k: 'n_neg',      t: '负年份',     c: nneg, asc0: 1, f: nnf },
    { k: 'is_',        t: 'IS',         c: v => cls(v), f: v => PCT(v) },
    { k: 'oos',        t: 'OOS',        c: v => cls(v), f: v => PCT(v) },
  ];
}

function tuneSorted() {
  const { k, asc } = TUNE_SORT;
  if (!k || !TUNE) return (TUNE || {}).rows || [];
  const val = x => {
    const y = x[k];
    return (y === null || y === undefined || !isFinite(y)) ? -Infinity : y;
  };
  return TUNE.rows.slice().sort((a, b) => asc ? val(a) - val(b) : val(b) - val(a));
}

function tuneSortBy(k) {
  const col = tuneCols(!!TUNE.cap_mode).find(c => c.k === k);
  if (!col) return;
  if (TUNE_SORT.k === k) TUNE_SORT.asc = !TUNE_SORT.asc;
  else TUNE_SORT = { k, asc: !!col.asc0 };
  paintTuneTable();
}

function paintTuneTable() {
  const cols = tuneCols(!!TUNE.cap_mode);
  const rows = tuneSorted().slice(0, 40);
  const th = `<th>#</th><th style="text-align:left">组合</th>` + cols.map(c =>
    `<th class="srt" data-k="${c.k}" title="点击按「${c.t}」排序">${c.t}`
    + (TUNE_SORT.k === c.k ? (TUNE_SORT.asc ? ' ▲' : ' ▼') : '')
    + `</th>`).join('')
    + `<th title="把这行的参数写回左侧面板">操作</th>`;
  /* ⚠️ 用 data-ri（行在 rows 里的下标）而不是闭包捕获 —— 表格是 innerHTML 重建的，
     每次重排后都必须重新绑定；用下标可以让「重绘 → 重绑」这一步是幂等的。 */
  const tb = rows.map((x, i) => `<tr class="${i === 0 ? 'top1' : i < 3 ? 'top2' : ''}">
      <td class="l"><span class="${i === 0 ? 'rk g' : i < 3 ? 'rk s' : 'rk'}">${i + 1}</span></td>
      <td class="l" style="white-space:normal;max-width:310px;font-size:12px">${esc(x.label)}</td>`
    + cols.map(c => `<td class="${c.c ? c.c(x[c.k], x) : ''}"${c.b ? ' style="font-weight:600"' : ''}>`
        + `${c.f(x[c.k], x)}</td>`).join('')
    + `<td><button class="btn ghost tiny tune-apply" data-ri="${i}"
         title="把「${esc(x.label)}」的参数写回左侧面板（不自动运行）">应用</button></td>`
    + `</tr>`).join('');
  const w = $('#tune-tblwrap');
  if (!w) return;
  w.innerHTML = `<div class="tw" style="max-height:520px;overflow-y:auto"><table><thead><tr>`
    + th + `</tr></thead><tbody>` + tb + `</tbody></table></div>`;
  $$('#tune-tblwrap th.srt').forEach(e =>
    e.addEventListener('click', () => tuneSortBy(e.dataset.k)));
  $$('#tune-tblwrap .tune-apply').forEach(b =>
    b.addEventListener('click', () => applyTuneRow(+b.dataset.ri)));
}

/* 把寻优某一行的参数写回左侧面板。
   ⚠️ `/api/tune` 的每行已经带 `params`（完整参数对象），**不需要**反推或重算 ——
      直接用即可。但要注意它只含 params，**不含 pool**：股票池是「当前页面正在用的」，
      寻优本身就是在该池下跑的，所以不该也不能被寻优结果覆盖（否则会把用户的池改掉）。 */
function applyTuneRow(ri) {
  const rows = tuneSorted();
  const x = rows[ri];
  if (!x || !x.params) { alert('这一行没有可用的参数'); return; }
  applyParams(x.params);
  // CFG 必须同步（它被 updateModeBadge / 徽标文案读取），否则界面显示与参数不一致
  CFG = { ...DEFAULT_PARAMS, ...x.params };
  updateModeBadge(); updateFinBadge(); updateDelistBadge();
  // 不自动跑：用户常要连点几行比较，每次重算会很卡（回测 + 选股要好几秒）
  setTuneStat(`已应用第 ${ri + 1} 行：${x.label} —— 参数已写回左侧，`
    + `点「运行回测」查看完整结果。`);
}

function renderTune(r) {
  if (!r.rows || !r.rows.length) {
    $('#tune-out').innerHTML = `<div class="empty">无有效组合</div>`;
    TUNE = null;
    const bt0 = $('#tune_apply_top'); if (bt0) bt0.disabled = true;
    setTuneStat('没有可用组合，无法应用', 'bad');
    return;
  }
  TUNE = r;
  // 有结果了才让「应用最优」可点（否则点了也没东西可应用）
  const bt = $('#tune_apply_top'); if (bt) bt.disabled = false;
  setTuneStat('');
  const cap = !!r.cap_mode;
  TUNE_SORT = { k: r.sort_by || (cap ? 'cap_sharpe' : 'sharpe'), asc: false };
  const SORT_NAME = {
    cap_sharpe: '账户资金口径 Sharpe', cap_cagr: '账户资金口径 年化',
    inv_cagr: '已投资金口径 年化', sharpe: '不限仓位 Sharpe', cagr: '不限仓位 年化',
  };
  const grid = r.n_grid && r.n_grid > r.n_used
    ? `网格 <b>${NUM(r.n_grid)}</b> → 等步长抽样 <b>${NUM(r.n_used)}</b>` : '';
  let h = `<div class="kv"><span>有效组合 <b>${NUM(r.n_combo)}</b>${grid ? ' · ' + grid : ''}</span>
    <span>耗时 <b>${r.ms} ms</b></span>
    <span>评估口径 <b style="font-family:inherit">${cap ? '容量约束（实盘可执行）' : '不限仓位（研究报告）'}</b></span>
    <span>排序依据 <b style="font-family:inherit">${SORT_NAME[r.sort_by] || r.sort_by} 降序</b></span></div>`;
  // 「为什么好几行数字一模一样」——容量口径下极常见的现象，不解释会以为是 bug。
  const dup = cap && r.n_distinct_cap !== undefined && r.n_distinct_cap !== null
    && r.n_distinct_cap < r.n_combo;
  if (dup) {
    h += `<div class="warnbox">⚠️ <b>${r.n_combo}</b> 个组合只给出了 <b>${r.n_distinct_cap}</b> 种不同的账户净值
      （最多 <b>${r.max_same_cap}</b> 个组合撞在一起）。这不是 bug：<br>
      仓位上限只有 N 个，而「最超跌优先」这类规则<b>永远先从最窄的那一档挑</b> ——
      只要最窄那档当天够 N 只，把区间放宽到 D1~D10 也<b>一只都多买不到</b>。<br>
      想让信号维度真正起作用，请把<b>「超额选股规则」也勾上</b>，或换成<b>不嵌套</b>的区间
      （如 D2~D3）。</div>`;
  }
  // 单独勾「超额选股规则」时，后端会自动补一个默认仓位 —— 必须说明，
  // 否则用户不知道表格里为什么突然多了「10 只 / 日 3 只」的约束。
  if (r.auto_cap) {
    h += `<div class="warnbox">ℹ️ 只勾了<b>超额选股规则</b>：不限仓位时「先买哪只」不影响任何结果
      （信号全买），所以本页<b>自动按「等权 10 只 / 日 3 只」评估</b>。
      想换别的仓位，请把<b>建仓节奏</b>也勾上。</div>`;
  }
  h += `<div class="card"><h3>寻优结果 <span class="tag">Top ${Math.min(40, r.rows.length)}</span>
      <span class="tag">点表头换排序</span></h3>
    <div class="desc">${cap
      ? `按<b>账户资金口径</b>评估 —— 即「你的账户里真实看到的收益率」，已经扣掉闲置资金。
         <br>⚠️ 账户 CAGR 会随「同时持仓上限」<b>机械升高</b>（钱铺得越开、闲置越少），
         所以别只挑 CAGR 最高的：要连<b>平均持仓</b>（真能盯住几只）、
         <b>账户 Sharpe / MDD</b>、<b>负年份</b>一起看。`
      : `按<b>不限仓位</b> Sharpe 排序（研究报告口径，信号全买 —— 实测平均同时持仓 900+ 只，实盘做不到）。
         报告的核心结论是<b>越简单越好</b>：请优先看「条件数少 + 负年份少 + OOS 仍为正」的组合，
         而不是单纯 Sharpe 最高的。想看实盘能执行的版本，请勾上<b>建仓节奏</b>。`}</div>
    <div id="tune-tblwrap"></div></div>`;
  const yc = tuneSorted().slice(0, 24);
  h += `<div class="card"><h3>${cap ? '账户 Sharpe' : 'Sharpe'} vs ${cap ? '账户' : ''}年化收益
      <span class="tag">前 24 个组合</span></h3>
    <div class="chart tall" id="ch-tune"></div>
    <div class="cap">气泡大小 = 信号数。右上角为更优区域。</div></div>`;
  $('#tune-out').innerHTML = h;
  paintTuneTable();
  const c = mkChart('ch-tune');
  const X = cap ? 'cap_cagr' : 'cagr', Y = cap ? 'cap_sharpe' : 'sharpe', D = cap ? 'cap_mdd' : 'mdd';
  c && c.setOption({
    grid: { left: 56, right: 20, top: 20, bottom: 42 },
    tooltip: { ...TIP, formatter: p => `<b>${p.data[3]}</b><br>
      ${cap ? '账户' : ''}年化 ${p.data[0].toFixed(1)}% · Sharpe ${p.data[1].toFixed(2)}<br>
      回撤 ${p.data[4].toFixed(1)}% · 信号 ${NUM(p.data[2])}` },
    xAxis: { type: 'value', name: `${cap ? '账户' : ''}年化收益 %`,
      nameTextStyle: { fontSize: 11, color: '#8d94a3' }, ...AX, nameLocation: 'middle', nameGap: 26 },
    yAxis: { type: 'value', name: 'Sharpe', nameTextStyle: { fontSize: 11, color: '#8d94a3' }, ...AX },
    series: [{
      type: 'scatter',
      data: yc.map(x => [+(((x[X] ?? 0)) * 100).toFixed(1), +((x[Y] ?? 0)).toFixed(2),
        x.n_signal, x.label, (x[D] ?? 0) * 100]),
      symbolSize: d => Math.max(7, Math.min(26, Math.sqrt(d[2]) / 26)),
      itemStyle: { color: 'rgba(50,102,173,.62)', borderColor: '#3266ad', borderWidth: 1 }
    }]
  });
}

/* 节奏轴的说明文字由后端 /api/defaults 的 rhythm_cands 生成 ——
   前端不自己维护一份候选，否则会出现「说明写了 8 档、实际跑了 10 档」。 */
function fillRhythmAxis(cands) {
  const el = $('#ax_rhythm_d');
  if (!el || !cands || !cands.length) return;
  const eq = cands.filter(c => !c.ladder).length;
  const lad = cands.length - eq;
  el.textContent = `等权 ${eq} 档 + 阶梯 ${lad} 档（共 ${cands.length} 档）`;
}


/* ---------------- 单股透视 ----------------
   ⚠️ 必须把**当前左侧参数 + 股票池**一起发过去（POST），不能用 GET 走默认参数。
      真实 bug：用户把市值放宽到「最小40%」后主面板显示 001256.SZ 有信号，
      而本页当时按写死的默认（最小30%、不限池）算 → 同一界面自相矛盾。
   ⚠️ 信号表**列全历史**（用户明确要求），图表默认近 250 日；
      不在图表范围内的信号**标灰 + 注明**，绝不隐藏。 */
let STK = null;          // 最近一次查询的完整响应（供「全部历史」切换重绘）
let STK_FULL = false;    // 用户是否要求了全历史图表
async function loadStock() {
  const code = $('#stk_code').value.trim();
  if (!code) return;
  $('#stk-out').innerHTML = `<div class="card"><div class="empty"><span class="spin"></span> 查询中…</div></div>`;
  STK_FULL = false;      // 换股票 → 回到默认窗口视图
  try {
    const d = await api('/api/stock', { code, params: collect(), pool: currentPool() });
    STK = d;
    renderStock(d);
  } catch (e) {
    STK = null;
    $('#stk-out').innerHTML = `<div class="errbox"><b>查询失败：</b>${e.message}</div>`;
  }
}
/* 切到全历史图表：懒加载（hist_rows 有 800~2900 行，不必每次查询都拉） */
async function loadStockFull() {
  if (!STK) return;
  if (STK.hist_rows) { STK_FULL = true; renderStock(STK); return; }
  const btn = $('#stk_full');
  if (btn) { btn.disabled = true; btn.textContent = '加载中…'; }
  try {
    const d = await api('/api/stock', { code: STK.code, params: collect(),
                                        pool: currentPool(), full_history: true });
    STK = d; STK_FULL = true;
    renderStock(d);
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = '全部历史'; }
    const b = $('#stk-fullerr');
    if (b) b.textContent = '全历史加载失败：' + e.message;
  }
}
$('#stk_go').addEventListener('click', loadStock);
$('#stk_code').addEventListener('keydown', e => { if (e.key === 'Enter') loadStock(); });

function renderStock(d) {
  const R = d.rows || [];
  if (!R.length) { $('#stk-out').innerHTML = `<div class="empty">无数据</div>`; return; }
  const sig = d.signal_dates || [];           // **全历史**信号
  const conds = d.conditions || [];
  const WIN = d.window || 250;
  const span = d.hist_span || [];
  const FULL = STK_FULL && Array.isArray(d.hist_rows) && d.hist_rows.length > 0;
  const nWin = d.n_signal_window != null ? d.n_signal_window : sig.filter(s => s.in_window).length;
  const nOut = sig.length - nWin;
  let h = `<div class="kv"><span>代码 <b>${d.code}</b></span>
    <span>名称 <b style="font-family:inherit">${d.name}</b></span>
    <span>行业 <b style="font-family:inherit">${d.ind}</b></span>
    <span>全历史${span.length === 2 ? `（${span[0]} ~ ${span[1]}）` : ''}触发条件
      <b>${sig.length}</b> 次</span></div>`;

  /* 本页用的是哪套条件 —— 用户必须能一眼对照主面板（否则两页数字对不上时无从解释） */
  h += `<div class="card" style="margin:10px 0"><h3>本页所用条件
      <span class="tag">跟随左侧参数</span></h3>
    <div class="desc">${conds.length
      ? conds.map(c => `<span class="pill">${esc(c)}</span>`).join(' ')
      : '<b>无条件</b>（全部股票 / 全部交易日都会被算作信号）'}</div>
    <div class="cap">${poolSummary(d.pool)}。改左侧任意参数后<b>重新查询</b>，
      本页信号会跟着变 —— 与主面板、交易明细用的是同一套条件。</div></div>`;

  const chartTag = FULL ? `全部历史${span.length === 2 ? ` · ${span[0]} ~ ${span[1]}` : ''}`
                        : `近 ${WIN} 交易日`;
  h += `<div class="charts">
    <div class="card" style="margin:0"><h3>价格与均线 <span class="tag">${esc(chartTag)}</span>
      <span style="flex:1"></span>
      <button class="btn2" id="stk_full" style="flex:0 0 auto">${FULL ? '近 250 日' : '全部历史'}</button>
    </h3>
      <div class="chart" id="ch-stk"></div>
      <div class="cap" id="stk-fullerr" style="color:#a83232"></div>
      <div class="cap">${nOut
        ? `图中 <span class="dotmark"></span> 为信号位置。<b>${nOut}</b> 个信号不在
           ${FULL ? '当前' : '近 ' + WIN + ' 日'}范围内${FULL ? '' : ' —— 点「全部历史」可看全'}。`
        : `图中 <span class="dotmark"></span> 为信号位置（全部落在当前范围内）。`}</div>
    </div>
    <div class="card" style="margin:0"><h3>距MA60 / 量比 <span class="tag">${esc(chartTag)}</span></h3>
      <div class="chart" id="ch-stk2"></div></div>
  </div>`;

  if (sig.length) {
    const w = sig.filter(s => s.fwd20 !== null);
    const avg = f => w.length ? w.reduce((a, x) => a + x[f], 0) / w.length : null;
    const win = w.length ? w.filter(x => x.fwd20 > 0).length / w.length : null;
    h += `<div class="mgrid" style="margin:14px 0">
      <div class="mc"><div class="l">信号次数（全历史）</div><div class="v sm">${sig.length}</div>
        <div class="n">最近一次 ${sig[sig.length - 1].date}</div></div>
      <div class="mc"><div class="l">信号后 5 日均值</div>
        <div class="v sm ${cls(avg('fwd5'))}">${F(avg('fwd5'), 2, '%')}</div></div>
      <div class="mc"><div class="l">信号后 20 日均值</div>
        <div class="v sm ${cls(avg('fwd20'))}">${F(avg('fwd20'), 2, '%')}</div></div>
      <div class="mc"><div class="l">信号后 60 日均值</div>
        <div class="v sm ${cls(avg('fwd60'))}">${F(avg('fwd60'), 2, '%')}</div></div>
      <div class="mc"><div class="l">20 日胜率</div>
        <div class="v sm">${win === null ? '—' : (win * 100).toFixed(1) + '%'}</div></div>
    </div>`;
    /* ⚠️ 均值为「—」的常见原因是**持有期还没走完**（末尾信号没有 20/60 日后的数据），
       不是 bug —— 必须说清楚，否则用户会以为算错了。 */
    const pend = sig.length - w.length;
    h += `<div class="card"><h3>历史信号明细
        <span class="tag">全历史 ${sig.length} 次 · 上述条件下触发</span></h3>
      <div class="row" style="max-width:420px;margin-bottom:8px">
        <input type="text" id="stk_yf" placeholder="按年份或日期筛选，如 2023 / 2026-09">
        <span class="btn2" id="stk_yfclr" style="flex:0 0 auto">清除</span>
        <span class="cap" id="stk_yfnote" style="flex:0 0 auto;margin:0"></span>
      </div>
      <div class="tw" style="max-height:360px;overflow-y:auto"><table><thead><tr>
      <th style="text-align:left">日期</th><th>距MA60</th><th>后 5 日</th>
      <th>后 20 日</th><th>后 60 日</th><th>图示</th></tr></thead><tbody id="stk_tb">
      ${stockRowsHTML(sig)}
      </tbody></table></div>
      <div class="cap">
        ${pend > 0 ? `${pend} 条信号的持有期<b>尚未走完</b>（越靠近最新交易日，
          20/60 日后的数据越可能还没产生），故右侧为「—」，<b>不计入</b>上面的均值与胜率。<br>` : ''}
        这是单只股票的历史样本，样本量小，不能据此判断「这只股票靠谱」。
        报告的核心结论是<b>用一篮子股票分散</b>，而不是押注单只。</div></div>`;
  } else {
    h += `<div class="warnbox">该股<b>全历史</b>（${span.length === 2 ? span[0] + ' ~ ' + span[1] : '全部交易日'}）
      内<b>未触发</b>上述条件。常见原因：① 它从未落到「距MA60 D1」那档；
      ② 市值不在你选的区间内；③ 它不在当前股票池里；④ 触发那几天市场不在你选的状态（如「熊市」）。<br>
      <b>提示：</b>若你在主面板看到它有信号、这里却看不到，请确认左侧参数
      与当时一致（尤其是<b>市值区间</b>和<b>股票池</b>），然后重新查询。</div>`;
  }
  $('#stk-out').innerHTML = h;

  /* 图表数据源：默认近 WINDOW 日；FULL 时用全历史 */
  const H = FULL ? d.hist_rows : R;
  const dates = H.map(x => x.date);
  const sigSet = new Set(FULL ? (d.hist_sig_dates || []) : sig.map(s => s.date));
  // 信号标记：与日期轴等长的数组，命中位置给收盘价（画散点），否则 null
  const sigMark = dates.map((dt, j) => (sigSet.has(dt) ? H[j].close : null));
  const iv = Math.max(1, Math.floor(dates.length / 8));
  const c1 = mkChart('ch-stk');
  c1 && c1.setOption({
    grid: { left: 50, right: 16, top: 30, bottom: 24 },
    tooltip: { trigger: 'axis', ...TIP },
    legend: { top: 2, right: 6, itemWidth: 12, itemHeight: 8,
      textStyle: { fontSize: 11, color: '#5c6270' } },
    xAxis: { type: 'category', data: dates, ...AX, boundaryGap: false,
      axisLabel: { ...AX.axisLabel, interval: iv } },
    yAxis: { type: 'value', scale: true, ...AX },
    series: [
      { name: '收盘', type: 'line', showSymbol: false, data: H.map(x => x.close),
        lineStyle: { width: 1.8, color: '#3266ad' }, itemStyle: { color: '#3266ad' } },
      // MA20/MA60 只有近 WINDOW 日有值；全历史图里隐藏（否则会画出一条从 0 起的假线）
      ...(FULL ? [] : [
        { name: 'MA20', type: 'line', showSymbol: false, data: R.map(x => x.ma20),
          lineStyle: { width: 1, color: '#b8860b' }, itemStyle: { color: '#b8860b' } },
        { name: 'MA60', type: 'line', showSymbol: false, data: R.map(x => x.ma60),
          lineStyle: { width: 1, color: '#6b5fc4' }, itemStyle: { color: '#6b5fc4' } }
      ]),
      { name: '信号', type: 'scatter', data: sigMark, symbolSize: 9,
        itemStyle: { color: '#d92b2b', borderColor: '#fff', borderWidth: 1 },
        z: 10, tooltip: { formatter: p => p.value == null ? '' : `信号 ${p.name}` } }
    ]
  });
  const c2 = mkChart('ch-stk2');
  c2 && c2.setOption({
    grid: { left: 50, right: 46, top: 30, bottom: 24 },
    tooltip: { trigger: 'axis', ...TIP },
    legend: { top: 2, right: 6, itemWidth: 12, itemHeight: 8,
      textStyle: { fontSize: 11, color: '#5c6270' } },
    xAxis: { type: 'category', data: dates, ...AX, boundaryGap: false,
      axisLabel: { ...AX.axisLabel, interval: iv } },
    yAxis: [
      { type: 'value', name: '距MA60 %', nameTextStyle: { fontSize: 10, color: '#8d94a3' },
        ...AX, axisLabel: { ...AX.axisLabel, formatter: v => v + '%' } },
      { type: 'value', name: '量比', nameTextStyle: { fontSize: 10, color: '#8d94a3' },
        ...AX, splitLine: { show: false } }
    ],
    series: [
      { name: '距MA60', type: 'line', showSymbol: false, data: H.map(x => x.dist60),
        lineStyle: { width: 1.6, color: '#e04a4a' }, itemStyle: { color: '#e04a4a' },
        areaStyle: { color: 'rgba(224,74,74,.08)' },
        // FULL 时叠加信号（用距MA60 的 y 值，落在同一条曲线上）
        markPoint: FULL ? { symbolSize: 7, data: dates.map((dt, j) =>
            sigSet.has(dt) && H[j].dist60 != null ? { coord: [j, H[j].dist60] } : null)
            .filter(Boolean),
          itemStyle: { color: '#d92b2b' } } : undefined },
      ...(FULL ? [] : [{ name: '量比', type: 'line', yAxisIndex: 1, showSymbol: false,
        data: R.map(x => x.rvol), lineStyle: { width: 1.1, color: '#1d9e75', type: 'dashed' },
        itemStyle: { color: '#1d9e75' } }])
    ]
  });
  /* 表格筛选 + 全历史切换按钮（必须在 innerHTML 之后绑定） */
  const yf = $('#stk_yf');
  if (yf) {
    yf.addEventListener('input', () => stockFilter(yf.value));
    $('#stk_yfclr').addEventListener('click', () => { yf.value = ''; stockFilter(''); });
  }
  const fb = $('#stk_full');
  if (fb) fb.addEventListener('click', () => {
    if (FULL) { STK_FULL = false; renderStock(STK); }
    else loadStockFull();
  });
}

/* 信号明细行（倒序：最新在上）。⚠️ in_window=false 的行**标灰 + 注明**，不隐藏。 */
function stockRowsHTML(rows) {
  return rows.slice().reverse().map(s => {
    const out = s.in_window === false;
    return `<tr class="${out ? 'stockrow-out' : ''}" data-d="${esc(s.date)}">
      <td class="l">${s.date}${out ? ' <span class="tag2">图外</span>' : ''}</td>
      <td class="${cls(s.dist60)}">${F(s.dist60, 2, '%')}</td>
      <td class="${cls(s.fwd5)}">${F(s.fwd5, 2, '%')}</td>
      <td class="${cls(s.fwd20)}">${F(s.fwd20, 2, '%')}</td>
      <td class="${cls(s.fwd60)}">${F(s.fwd60, 2, '%')}</td>
      <td>${out ? '<span class="dimnote">不在当前图表范围</span>' : '●'}</td></tr>`;
  }).join('');
}
/* 按年份 / 日期前缀筛选明细行（纯前端，不改数据） */
function stockFilter(q) {
  q = String(q || '').trim();
  const tb = $('#stk_tb');
  if (!tb) return;
  let n = 0;
  [...tb.querySelectorAll('tr')].forEach(tr => {
    const hit = !q || (tr.dataset.d || '').startsWith(q);
    tr.style.display = hit ? '' : 'none';
    if (hit) n++;
  });
  const fb = $('#stk_yfnote');
  if (fb) fb.textContent = q ? `筛选「${q}」：${n} 条` : '';
}

/* ---------------- 研究结论 ---------------- */
function helpHTML() {
  return `
  <div class="card"><h3>研究结论摘要 <span class="tag">V3B · 2015-2026 · 5304 只 · 1053 万行</span></h3>
    <div class="desc">以下是本界面所有默认参数的来源。全部经横截面十分位 + 日加权超额 + t 检验验证。</div>
    <div class="okbox"><b>最值得关注的情况（3 个条件）：</b><br>
      ① 距MA60 处于当日横截面 <b>D1</b>（最弱 10%）&nbsp; ② 市值处于<b>最小 30%</b>&nbsp;
      ③ 全A等权净值 <b>&lt; 其 MA60</b>（熊市）&nbsp; → &nbsp;持有 20 日<br>
      实测 CAGR <b>29.9%</b> / MDD <b>−41.3%</b> / Sharpe <b>0.98</b> /
      胜率 <b>58.5%</b> / PF <b>2.19</b> / 正年份 <b>10/12</b> / 四段分期全为正</div>

    <div class="sec-title">五个「不要做」</div>
    <div class="tw"><table><thead><tr><th style="text-align:left">做法</th>
      <th>实测结果</th><th style="text-align:left">原因</th></tr></thead><tbody>
      <tr><td class="l">等「反转确认」信号</td><td class="neg">8 个信号全负</td>
        <td class="l" style="font-family:inherit">反转从 T+1 就开始</td></tr>
      <tr><td class="l">连续 2 日上涨再买</td><td class="neg">−3.421% (t −33.96)</td>
        <td class="l" style="font-family:inherit">最差信号，涨完才确认</td></tr>
      <tr><td class="l">放量做确认（量比&gt;2.5）</td><td class="neg">−0.854%</td>
        <td class="l" style="font-family:inherit">放量是反向指标</td></tr>
      <tr><td class="l">要求「相对强势」</td><td class="neg">−1.103%</td>
        <td class="l" style="font-family:inherit">超跌 + 强势是最差组合</td></tr>
      <tr><td class="l">剔除最小市值</td><td class="neg">+0.219% → +0.103%</td>
        <td class="l" style="font-family:inherit">小市值是收益主要来源</td></tr>
    </tbody></table></div>

    <div class="sec-title">必须知道的保留</div>
    <div class="warnbox"><b>收益归因：约 2/3 来自小市值 beta，1/3 来自超跌 alpha。</b><br>
      · 叠加版 − 小市值单独 = <b>+6.3pp</b>（超跌的真实增量）<br>
      · 叠加版 − 距MA60单独 = <b>+13.3pp</b>（小市值的增量）<br>
      · 「距MA60 D1 + 大市值」只有 CAGR <b>2.9%</b>、Sharpe <b>0.26</b>
      —— <b>没有小市值，超跌几乎不赚钱</b>。<br>
      · 2015-2026 恰好是小市值占优期，这是真实的样本外风险（2017 年 −12%、2018 年 −17%）。</div>

    <div class="warnbox"><b>用户原设想的 8 个策略：不成立。</b>
      其中 6 个劣于「什么都不加」的基准（纯价格超跌 CAGR 10.7%）。
      逐条反向修正后的 7 个版本才全部显著更优。<br>
      另外「D2~D6 最优」只在 ret20、ret5/ret10 成立；
      在 ret40 / ret60 / 距MA60 / 距MA120 上，<b>最优就是最极端的 D1</b>（单调递减）。</div>

    <div class="warnbox"><b>两个已修正的方法学错误（影响历史结论量级）：</b><br>
      ① <b>收益区间错位</b>：用 T日开盘→T+1开盘 的口径回测 T日收盘生成的信号，
      会把超跌股继续暴跌的那一天算进组合（实测差 1.37pp/日、方向相反）。<br>
      ② <b>块内秩单键排序</b>：使同一天元素被打散、秩算出负数，
      甚至在横截面常数因子上凭空造出 t+5.52 的假 IC 榜首。</div>

    <div class="cap" style="margin-top:14px;padding-top:12px;border-top:1px solid #e4e6ea">
      <b>免责声明：</b>本界面与全部回测结果仅为量化研究的技术演示，
      不构成任何投资建议或收益承诺。历史回测不代表未来表现。
      数据来自公开行情，可能存在复权、停牌、涨跌停等处理偏差。
      任何依据本工具做出的投资决策，风险由使用者自行承担。</div>
  </div>`;
}

/* ---------------- 事件绑定 ---------------- */
bindSeg('#seg-ma60'); bindSeg('#seg-ma120'); bindSeg('#seg-ret20');
bindSeg('#seg-rs'); bindSeg('#seg-ihv'); bindSeg('#seg-rv');
bindSeg('#seg-mkt'); bindSeg('#seg-hv'); bindSeg('#seg-pool');
bindSeg('#seg-ex', true);

/* 市场指数口径 / 均线窗口 —— ⚠️ 这两个参数**会改变 mask**（与容量参数不同），
   所以选股与回测都会失效。这里只**静默重跑选股**（顺带刷新「市场环境」表，约 1 秒）；
   回测较贵，交给用户点「运行回测」。 */
['#mkt_index', '#mkt_ma'].forEach(s => {
  const el = $(s);
  if (el) el.addEventListener('change', async () => {
    syncMktCfgNote();
    updateModeBadge();
    await runScan(true);
  });
});
/* ---- 市值区间双滑块 ----
 * 拖动时把**被拖的那个把手**夹住，而不是事后交换两个值：
 * 交换会让手指下的把手突然跳到对面，手感很差；夹住则允许两者重叠
 * （重叠 = 只取某一个分位，如「10%~20%」就是下限=上限=D2）。 */
function bindSizeSlider(id) {
  const el = $('#' + id);
  if (!el) return;
  el.addEventListener('input', () => {
    const lo = $('#size_min'), hi = $('#size_max');
    if (id === 'size_min' && Number(lo.value) > Number(hi.value)) lo.value = hi.value;
    if (id === 'size_max' && Number(hi.value) < Number(lo.value)) hi.value = lo.value;
    syncSize(); updateModeBadge();
  });
}
bindSizeSlider('size_min');
bindSizeSlider('size_max');
/* 市值区间快捷档位 */
$$('#size_quick button').forEach(b => b.addEventListener('click', () => {
  applySize(Number(b.dataset.smin), Number(b.dataset.smax));
  updateModeBadge();
}));
$('#hold').addEventListener('input', syncLabels);
$('#deep_any').addEventListener('change', updateModeBadge);
$$('#confirms input').forEach(i => i.addEventListener('change', updateModeBadge));
/* ---- 财务筛选 ---- */
$$('.finrow input[type=number]').forEach(i => i.addEventListener('input', updateFinBadge));
$('#fin_period').addEventListener('change', updateFinBadge);
$$('.finquick button').forEach(b => b.addEventListener('click', () => {
  setFinQuick(b.dataset.fin);
}));
/* ---- 退市风险过滤 ---- */
['#dl_fin', '#dl_loss2y', '#dl_penny', '#dl_main2024'].forEach(s => {
  const e = $(s); if (e) e.addEventListener('change', updateDelistBadge);
});
['#dl_rev_floor', '#dl_penny_days'].forEach(s => {
  const e = $(s); if (e) e.addEventListener('input', updateDelistBadge);
});
/* ---------------- 容量约束（实盘可执行性） ---------------- */
/* 口径单选：不限仓位 / 限制持仓。选「限制持仓」时才把容量参数发给后端。 */
$$('#seg-cap button').forEach(b => b.addEventListener('click', () => {
  b.classList.add('on');
  $$('#seg-cap button').forEach(x => { if (x !== b) x.classList.remove('on'); });
  syncCap();
}));
/* 快捷预设：同时持仓,每日买入 */
$$('.capquick button').forEach(b => b.addEventListener('click', () => {
  const [mp, mn] = String(b.dataset.cap).split(',').map(Number);
  // ⚠️ 快捷预设本身就是「等权」配置 —— 必须先关掉建仓节奏，
  //    否则两个输入框还处于禁用接管态，改不动（改了也会和阶梯口径打架）。
  const rs = $('#cap_rhythm');
  if (rs) rs.value = '';
  // ⚠️ 顺序不能反：syncLadder() 切回等权时会把「接管前的原值」写回两个输入框，
  //    所以必须先回收、再覆盖为快捷预设值。
  syncLadder();
  $('#cap_maxpos').value = mp;
  $('#cap_maxnew').value = mn;
  // 点快捷预设 → 自动切到「限制持仓」
  $$('#seg-cap button').forEach(x => x.classList.toggle('on', x.dataset.v === '1'));
  syncCap();
  $$('.capquick button').forEach(x => x.classList.toggle('on', x === b));
}));
['#cap_maxpos', '#cap_maxnew'].forEach(s => $(s).addEventListener('input', () => {
  $$('.capquick button').forEach(x => x.classList.remove('on'));
  syncCap();
}));
$('#cap_pick').addEventListener('change', syncCap);
function syncCap() {
  const on = segv('#seg-cap') === '1';
  $('#capwrap').classList.toggle('on', on);
  $('#capwrap').style.display = on ? 'block' : 'none';
  const lad = on ? ladderNow() : null;
  const mp = parseInt($('#cap_maxpos').value, 10) || 10;
  const mn = parseInt($('#cap_maxnew').value, 10) || 3;
  const pk = $('#cap_pick');
  const pn = pk && pk.selectedOptions[0] ? pk.selectedOptions[0].textContent : '最超跌优先';
  const M = 'font-family:var(--mono)';
  const ladTxt = lad
    ? ` · 建仓节奏 <b style="${M}">${lad.join('/')}</b> 只/批`
      + `（满仓 ${lad.reduce((a, b) => a + b, 0)} 只，共 ${lad.length} 批）`
    : '';
  if (on) {
    $('#cap_stat').innerHTML = `<span style="color:#a8622c">当前口径：限制持仓（实盘可执行）</span>
      · 同时最多 <b style="${M}">${mp}</b> 只
      · ${lad
        ? `每日买入 <b style="${M}">由序列推出</b>`
        : `每日最多买 <b style="${M}">${mn}</b> 只`}
      · 超额时按「${pn}」排序${ladTxt}`;
  } else {
    $('#cap_stat').innerHTML = '当前口径：<b>不限仓位</b>（研究报告原始口径，'
      + '某日有多少信号就等权买多少只，实盘不可执行）';
  }
  const bd = $('#cap_badge');
  if (bd) {
    bd.textContent = on
      ? (lad ? `已启用 · 阶梯满仓${lad.reduce((a, b) => a + b, 0)}只`
             : `已启用 · ${mp}只/${mn}只`)
      : '已关闭';
  }
}
/* 填充选股规则下拉（来自 /api/defaults 的 pick_rules） */
function fillPickRules(rules) {
  const sel = $('#cap_pick'); if (!sel || !rules || !rules.length) return;
  sel.innerHTML = rules.map(r =>
    `<option value="${r.id}"${r.default ? ' selected' : ''}>${r.name} —— ${r.desc}</option>`
  ).join('');
}

/* ---------------- 市场指数口径 / 均线窗口 ----------------
   牛熊判定用的那条线是**自建净值**（不是交易所指数），由「一批股票的等权
   开盘→开盘日收益」累乘而成。这两个下拉决定「用哪批股票」和「比多少日均线」。

   ⚠️ 选项清单来自 /api/meta（`mkt_indexes` / `mkt_ma_windows`），前端**不自己维护**
      一份 —— 否则后端加了口径、前端下拉里没有，用户永远用不上。

   ⚠️ 换口径 / 换窗口**会改变 mask**（与容量参数不同），所以每次变更都要重跑。
      实测（reports/41_market_index_size.md）：小市值口径 CAGR 更高，但
        · 改善来源是「过滤变严」而非「指数更聪明」（MA120+全A ≈ MA60+小50%）；
        · 对窗口极度敏感 —— 小50% 在 MA20 下只有 16.81%，远差于全A 的 25.93%。
      所以默认保持研究报告口径（全A等权 / MA60），并在下方给出警告文案。 */
const MKT_NAME = {};    // 口径 key → 中文名（来自 /api/meta，供 paramSummary 复用）
function fillMktOptions(m) {
  const ix = (m && m.mkt_indexes) || [];
  const mw = (m && m.mkt_ma_windows) || [];
  const selI = $('#mkt_index'), selM = $('#mkt_ma');
  ix.forEach(x => { MKT_NAME[x.key] = x.name; });
  if (selI && ix.length) {
    selI.innerHTML = ix.map(x =>
      `<option value="${x.key}"${x.default ? ' selected' : ''}>${x.name} —— ${x.desc}</option>`
    ).join('');
  }
  if (selM && mw.length) {
    selM.innerHTML = mw.map(x =>
      `<option value="${x.ma}"${x.default ? ' selected' : ''}>${x.ma} 日</option>`
    ).join('');
  }
}

/* 当前口径的提示文案：非默认口径时给出「结论会变」的警告。
   ⚠️ MA 窗口的敏感性只在这里口头提示是不够的 —— 真正的数字在「市场环境」表里
      （mktBlock 会并列同一口径下三个窗口的距均线值），那里才是可验证的依据。 */
function syncMktCfgNote() {
  const el = $('#mkt_cfg_note'); if (!el) return;
  const ix = $('#mkt_index'), ma = $('#mkt_ma');
  if (!ix || !ma) return;
  const isDef = (ix.value === 'all' && String(ma.value) === '60');
  const nm = (ix.selectedOptions[0] || {}).textContent || ix.value;
  const short = nm.split(' —— ')[0];
  if (isDef) {
    el.style.color = '';
    el.innerHTML = '当前：<b>全A等权 / MA60</b>（研究报告口径，与真实指数最接近）。';
  } else {
    el.style.color = '#a8622c';
    el.innerHTML = `当前：<b>${esc(short)} / MA${esc(ma.value)}</b> —— `
      + '⚠️ 非默认口径。<b>换指数与换均线窗口的效果几乎等价</b>（都等于把过滤变严），'
      + '且小市值口径对窗口极度敏感（MA20 下反而远差于全A）。'
      + '下方的「市场环境」表会并列三个窗口的当前状态，请对照后再用。';
  }
}

/* ---------------- 建仓节奏（阶梯建仓） ----------------
   序列里的每个数 = **该批买入的股票只数**：满仓只数 = 序列之和，
   同时持仓上限与每日买入都由它推出（两个输入框会被接管置灰）。
   前端解析只用于**界面反馈**（画柱状示意、算满仓只数/每只资金）。
   发给后端的是**原始字符串**，由 engine.parse_ladder() 做权威解析 ——
   避免前后端两套规则漂移（例如后端偷偷支持全角逗号而前端不认）。 */
let LAD_PRESETS = [];
let LAD_AUTO_MP = null;      // 启用阶梯时被自动接管的「同时持仓」原值
let LAD_AUTO_MN = null;      // 启用阶梯时被自动接管的「每日买入」原值
function parseLadderStr(s) {
  const t = String(s == null ? '' : s).trim();
  if (!t) return null;
  const parts = t.replace(/[，、/|;；\s]+/g, ',').split(',').filter(x => x !== '');
  const v = [];
  for (const p of parts) {
    const n = Number(p);
    if (!isFinite(n)) return null;
    const c = Math.round(n);            // 只数按整数取（1.6 → 2）
    if (c < 1) return null;             // 每批至少 1 只
    v.push(c);
  }
  return (v.length && v.length <= 50) ? v : null;
}
/* 当前生效的阶梯（未启用/非法 → null） */
function ladderNow() {
  const r = $('#cap_rhythm');
  if (!r || r.value === '') return null;
  return parseLadderStr($('#cap_ladder').value);
}
function fillLadderPresets(list) {
  const sel = $('#cap_rhythm');
  if (!sel || !list || !list.length) return;
  LAD_PRESETS = list;
  const cur = sel.value;
  sel.innerHTML = '<option value="">等权（每笔一样大）</option>'
    + list.map(p => `<option value="${p.ladder.join(',')}">阶梯 ${p.ladder.join(' / ')}`
        + (p.tag ? ` · ${esc(p.tag)}` : '') + '</option>').join('')
    + '<option value="__custom__">自定义…</option>';
  if ([...sel.options].some(o => o.value === cur)) sel.value = cur;
  syncLadder();
}
function syncLadder() {
  const sel = $('#cap_rhythm'), row = $('#cap_ladder_row');
  const inp = $('#cap_ladder'), box = $('#cap_ladder_info');
  const mpEl = $('#cap_maxpos'), mnEl = $('#cap_maxnew');
  if (!sel || !row || !inp || !box) return;
  const raw = sel.value;
  const show = raw !== '';
  row.style.display = show ? '' : 'none';
  box.style.display = show ? '' : 'none';
  if (!show) {                        // 回到等权：归还「同时持仓」「每日买入」
    if (mpEl) {
      if (LAD_AUTO_MP !== null) { mpEl.value = LAD_AUTO_MP; LAD_AUTO_MP = null; }
      mpEl.disabled = false; mpEl.title = '';
    }
    if (mnEl) {
      if (LAD_AUTO_MN !== null) { mnEl.value = LAD_AUTO_MN; LAD_AUTO_MN = null; }
      mnEl.disabled = false; mnEl.title = '';
    }
    return;
  }
  if (raw !== '__custom__' && raw !== inp.value) inp.value = raw;

  const lad = parseLadderStr(inp.value);
  inp.classList.toggle('bad', !lad);
  if (!lad) {                         // 打字中间态：如实提示，不打断输入
    box.innerHTML = '<div class="ladsum" style="color:#c0392b">只数序列无效'
      + '（需为 ≥1 的整数，逗号或斜杠分隔），当前按<b>等权</b>处理。</div>';
    if (mpEl) { mpEl.disabled = false; mpEl.title = ''; }
    if (mnEl) { mnEl.disabled = false; mnEl.title = ''; }
    return;
  }
  const tot = lad.reduce((a, b) => a + b, 0);      // 满仓只数 = 序列之和
  const mx = Math.max(...lad);
  // 满仓只数 → 同时持仓上限；最大单批只数 → 每日买入上限（都为推导值）
  if (mpEl) {
    if (!mpEl.disabled) LAD_AUTO_MP = mpEl.value;
    mpEl.value = tot;
    mpEl.disabled = true;
    mpEl.title = '由建仓节奏推出：满仓只数 = 序列之和，不能单独修改';
  }
  if (mnEl) {
    if (!mnEl.disabled) LAD_AUTO_MN = mnEl.value;
    mnEl.value = mx;
    mnEl.disabled = true;
    mnEl.title = '由建仓节奏推出：最大单批只数，不能单独修改';
  }

  const per = 100 / tot;                            // 每只占账户资金（%）
  const f = v => (+v.toFixed(2));
  let acc = 0;
  const cums = lad.map(x => (acc += x));
  box.innerHTML =
    '<div class="ladbars">' + lad.map((x, i) =>
      `<div class="ladbar" title="第 ${i + 1} 批买入 ${x} 只 · 累计 ${cums[i]} 只">`
      + `<span class="ladtrack"><i style="height:${Math.max(4, Math.round(x / mx * 100))}%"></i></span>`
      + `<em>${x}</em></div>`).join('') + '</div>'
    + `<div class="ladsum"><b>${tot}</b> 只满仓 · <b>${lad.length}</b> 批建仓`
    + ` · 每只 <b>${f(per)}%</b> 资金 · 每批 <b>${lad.join(' / ')}</b> 只</div>`
    + '<div class="ladsum">序列里的数就是<b>每批买入的只数</b> —— '
    + '满仓只数（= 序列之和）与每日买入都由它推出，'
    + '<b>不用再填「同时持仓」「每日买入」</b>。'
    + `每次建仓把持仓补到下一个累计目标（${cums.join(' → ')} 只），`
    + `买满 <b>${tot}</b> 只即停，等卖出释放额度后再补。`
    + '资金每只等分，<b>绝不超配</b>。</div>';
}
const _rhythm = $('#cap_rhythm');
if (_rhythm) _rhythm.addEventListener('change', () => {
  if (_rhythm.value === '__custom__') { $('#cap_ladder').focus(); $('#cap_ladder').select(); }
  syncLadder(); syncCap();
});
const _ladInp = $('#cap_ladder');
if (_ladInp) _ladInp.addEventListener('input', () => {
  // 手输后若与某个预设一致，下拉自动对上；否则切到「自定义」。
  // ⚠️ 空串必须走「自定义」而不是匹配到 value="" 的「等权」选项 ——
  //    用户在删除重打的过程中会短暂出现空串，若这时偷偷关掉阶梯会很突兀。
  const sel = $('#cap_rhythm'), v = _ladInp.value.trim();
  if (sel) sel.value = (v && [...sel.options].some(o => o.value === v)) ? v : '__custom__';
  syncLadder(); syncCap();
});
function syncIndCnt() {
  const n = $$('#inds input.ibx:checked').length;
  const el = $('#indcnt');
  if (el) el.textContent = n ? `（选中 ${n} 个）` : '（0 选中 = 不限）';
}
function syncBdCnt() {
  const n = $$('#seg-board button.on').length;
  $('#bdcnt').textContent = n ? `（选中 ${n} 个）` : '（0 选中 = 不限）';
}
/* 行业树的事件委托。
   ⚠️ 不能像原来那样在解析期用 $$('#inds input') 逐个挂监听：那些复选框是 init()
   异步渲染出来的，解析期 #inds 还是空的 → 一个监听都挂不上，
   表现为「单独勾一个行业时计数不更新」。委托到容器上就没这个问题。 */
const INDS = $('#inds');
if (INDS) {
  INDS.addEventListener('change', e => {
    const t = e.target;
    if (t.classList.contains('ibx')) {            // 细分行业
      syncGrpState(); syncIndCnt();
    } else if (t.classList.contains('igc')) {     // 门类 = 全选/全不选本组
      const g = t.closest('.igt');
      if (g) g.querySelectorAll('.ibx').forEach(i => { i.checked = t.checked; });
      syncGrpState(); syncIndCnt();
    }
  });
  // 点门类名称行 = 展开/收起（点复选框本身不触发，否则会和勾选打架）
  INDS.addEventListener('click', e => {
    if (e.target.tagName === 'INPUT') return;
    const head = e.target.closest('.igh');
    if (head) head.closest('.igt').classList.toggle('indopen');
  });
}
/* 行业搜索框 */
const IND_Q = $('#ind_q');
if (IND_Q) {
  IND_Q.addEventListener('input', () => filterInds(IND_Q.value));
  const clr = $('#ind_q_clr');
  if (clr) clr.addEventListener('click', () => {
    IND_Q.value = ''; filterInds(''); IND_Q.focus();
  });
}
/* ⚠️ 这里挂的是所有 .poolcls 按钮（行业 全选/清空/展开 与 板块 全选/清空 共用样式），
   必须按 data-act 分流，否则板块按钮会去勾行业复选框。 */
$$('.poolcls button').forEach(b => b.addEventListener('click', () => {
  const act = b.dataset.act;
  if (act === 'allbd' || act === 'nonebd') {
    const on = act === 'allbd';
    $$('#seg-board button').forEach(x => x.classList.toggle('on', on));
    syncBdCnt();
    return;
  }
  if (act === 'expandind') {
    // 有任意一组是收起的就全展开，否则全收起
    const all = $$('#inds .igt');
    const expand = !all.every(g => g.classList.contains('indopen'));
    all.forEach(g => g.classList.toggle('indopen', expand));
    return;
  }
  const on = act === 'allind';
  $$('#inds input.ibx').forEach(i => i.checked = on);
  syncGrpState();
  syncIndCnt();
}));
/* 板块是多选：点击即切换，不是单选 */
$$('#seg-board button').forEach(b => b.addEventListener('click', () => {
  b.classList.toggle('on');
  syncBdCnt();
}));
$$('#seg-pool button').forEach(b => b.addEventListener('click', () => {
  $('#poolcustom').style.display = segv('#seg-pool') === 'custom' ? 'block' : 'none';
}));
$('#pool_codes').addEventListener('input', () => {
  const t = $('#pool_codes').value.replace(/[,，]/g, ' ').trim();
  const n = t ? t.split(/\s+/).filter(Boolean).length : 0;
  $('#poolcnt').textContent = `已输入 ${n} 个代码`;
});
$('#pool_clear').addEventListener('click', () => {
  $('#pool_codes').value = ''; $('#poolcnt').textContent = '已输入 0 个代码';
});
$('#pool_load').addEventListener('click', () => {
  if (!SCAN || !SCAN.items || !SCAN.items.length) { alert('请先运行一次选股'); return; }
  $('#pool_codes').value = SCAN.items.map(x => x.code).join(', ');
  const n = SCAN.items.length;
  $('#poolcnt').textContent = `已输入 ${n} 个代码`;
  $$('#seg-pool button').forEach(b => b.classList.toggle('on', b.dataset.v === 'custom'));
  $('#poolcustom').style.display = 'block';
});
$('#preset').addEventListener('change', () => {
  const p = curPreset();
  if (!p) return;
  applyParams(p.params);
  applyPool(p.pool);          // 无 pool（内置预设 / 早期方案）→ 保持现有池子不动
  syncPresetUI();
  setPresetStat('');
  updateModeBadge(); runAll();
});
/* ---- 我的方案：保存 / 删除 ---- */
$('#preset_save').addEventListener('click', savePreset);
$('#preset_del').addEventListener('click', deletePreset);
/* ---- 设为默认 / 取消默认（当前选中的那套）---- */
$('#preset_def').addEventListener('click', () => {
  const p = curPreset();
  if (p) setDefaultPreset(p.id);
});
$('#preset_def_clear').addEventListener('click', clearDefaultPreset);
// 切方案时清掉提示（否则上一套的「已设为默认」会挂在新方案下面，误导）
$('#preset').addEventListener('change', () => setPresetStat(''));
// 回车 = 保存（输入框里按回车比移鼠标快）
$('#preset_name').addEventListener('keydown', e => {
  if (e.key === 'Enter') { e.preventDefault(); savePreset(); }
});
$('#preset_name').addEventListener('input', () => setPresetStat(''));
$('#run').addEventListener('click', () => { updateModeBadge(); runAll(); });
$('#reset').addEventListener('click', () => {
  const p0 = PRESETS[0];
  $('#preset').value = p0.id;
  applyParams(p0.params);
  syncPresetUI();
  setPresetStat('');
  // 股票池整体复位（默认池 = 全部A股 · 主板+创业板+科创板 · 沪深，北交所不选）
  applyPool(defaultPool());
  updateModeBadge(); runAll();
});
syncLabels(); updateModeBadge(); updateFinBadge(); updateDelistBadge();
$('#btn-upd') && $('#btn-upd').addEventListener('click', onUpdateClick);
init();
