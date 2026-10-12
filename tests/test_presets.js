/* 前端「② 市值区间」+「预设方案（我的方案）」验证（jsdom 无头）
 *
 * 覆盖两组新功能：
 *   A. 市值区间（双滑块）
 *      1. 控件齐全（下限/上限 range、读数、填充条、快捷档位）
 *      2. 默认 = 最小 30%（smin=0, smax=2），读数文案「最小30%」
 *      3. 拖动时把手互相夹住（不允许越过），绝不产生空集
 *      4. 两个把手重叠 = 单档筛选（如 10%~20% 就是 smin=smax=1）
 *      5. 快捷档位按钮写入并高亮；collect() 带出 size_min / size_max
 *      6. 徽标文案随区间变化（「市值最小30%」/「市值10%~20%」/ 全部时不计入）
 *   B. 我的方案（保存 / 删除）
 *      7. 下拉分「内置方案 / 我的方案」两组，user_presets 渲染进第二组
 *      8. 选中「我的方案」→ 参数写回控件、名称回填、删除按钮可用
 *      9. 名称为空 → 红字提示且**不发请求**
 *     10. 保存 → POST /api/presets/save，payload 带全量参数（含 size_min/size_max）
 *     11. 保存成功 → 新方案出现在下拉里并被选中
 *     12. 删除 → 确认后 POST /api/presets/delete，方案消失、回落首个内置方案
 *     13. 内置方案：删除按钮禁用、点了也不发请求
 *     14. 无运行时错误
 *   C. 股票池也要能存（回归：早期只存 params，pool 整组丢失）
 *     15. 保存请求带 pool：板块 / 行业 / 自定义代码 / 模式
 *     16. 切到无 pool 的内置预设 → 股票池原样不动（向后兼容）
 *     17. 改乱池子后切回「我的方案」→ 板块/行业/自定义代码完整回填
 *     18. 保存提示与预设描述里出现「股票池」摘要
 */
const fs = require('fs');
const path = require('path');
const ROOT = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'app/index.html'), 'utf8');
const { JSDOM } = require(path.join(ROOT, 'node_modules/jsdom'));
const F = n => JSON.parse(fs.readFileSync(path.join(__dirname, `fixtures/fixture_${n}.json`), 'utf8'));

const errors = [];
const reqs = [];

/* 起一个 jsdom；user_presets 由本测试自己造，从初始值开始，保存/删除就地更新，
 * 这样 save → 再 delete 的链路能在一次会话里闭环验证。 */
function boot(opts) {
  const o = opts || {};
  const FX = {
    meta: F('meta'), defaults: F('defaults'),
    backtest: F('backtest'), scan: F('scan'),
  };
  // 允许测试注入自己的「我的方案」与默认预设（默认取值来自 fixture）
  let users = (o.users || FX.defaults.user_presets || []).slice();
  // 「界面默认预设」的可变状态（模拟 data/default_preset.txt）
  let def = o.defaultPreset !== undefined ? o.defaultPreset
    : (FX.defaults.default_preset || '');
  const dom = new JSDOM(html, {
    runScripts: 'dangerously', pretendToBeVisual: true,
    url: 'http://127.0.0.1:8771/',
    beforeParse(w) {
      w.HTMLElement.prototype.scrollIntoView = () => {};
      w.requestAnimationFrame = cb => setTimeout(() => cb(Date.now()), 0);
      w.confirm = () => true;
      w.echarts = { init: () => ({ setOption() {}, resize() {}, dispose() {}, getDom: () => null }) };
      w.fetch = async (url, opt) => {
        const u = String(url);
        const method = (opt && opt.method) || 'GET';
        let payload = null;
        if (opt && opt.body) { try { payload = JSON.parse(opt.body); } catch (e) { /* 非 JSON */ } }
        reqs.push({ url: u, method, payload });
        let body = {};
        if (u.includes('/api/meta')) body = FX.meta;
        else if (u.includes('/api/defaults')) {
          body = { ...FX.defaults, user_presets: users, default_preset: def };
        } else if (u.includes('/api/presets/save')) {
          const nm = String(payload.name).trim();
          const hit = users.find(x => x.name === nm);
          if (hit) { hit.params = payload.params; hit.pool = payload.pool || null; }
          else users = users.concat([{
            id: 'U' + (users.length + 1), name: nm, desc: '',
            saved_at: '2026-09-26 02:20', params: payload.params,
            pool: payload.pool || null,
          }]);
          body = { ok: true, id: (hit ? hit.id : 'U' + users.length),
            name: nm, overwrote: !!hit,
            msg: hit ? `已覆盖同名方案「${nm}」` : `已保存方案「${nm}」`, presets: users };
        } else if (u.includes('/api/presets/delete')) {
          users = users.filter(x => x.id !== payload.id);
          body = { ok: true, msg: '已删除', presets: users };
        } else if (u.includes('/api/presets/set_default')) {
          // 与后端同语义：空 id = 取消；非空则校验存在性
          const pid = String(payload.id || '').trim();
          const known = pid === '' ||
            (FX.defaults.presets || []).some(x => x.id === pid) ||
            users.some(x => x.id === pid);
          if (!known) body = { ok: false, msg: '该方案不存在' };
          else { def = pid; body = { ok: true, default_preset: pid,
            msg: pid ? '已设为默认' : '已取消默认设置（将使用内置第一套）' }; }
        } else if (u.includes('/api/backtest')) body = FX.backtest;
        else if (u.includes('/api/scan')) body = FX.scan;
        else if (u.includes('/api/trades')) body = { rows: [], n_trade: 0, summary: {}, yearly: {} };
        else body = {};
        return {
          ok: true, status: 200, headers: { get: () => null },
          json: async () => body, text: async () => JSON.stringify(body),
          blob: async () => ({ size: 3 }), arrayBuffer: async () => new ArrayBuffer(3),
        };
      };
      w.URL.createObjectURL = () => 'blob:x';
      w.URL.revokeObjectURL = () => {};
      w.onerror = (m, s, l, c) => errors.push(`${m} @${l}:${c}`);
      w.console.error = (...a) => errors.push(a.join(' '));
    },
  });
  return { dom, w: dom.window, d: dom.window.document };
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

let pass = 0, fail = 0;
const ok = (c, m) => { if (c) pass++; else { fail++; console.log('  ✗ ' + m); } };

(async () => {
  const { w, d } = boot();
  await sleep(600);
  const $ = s => d.querySelector(s), $$ = s => [...d.querySelectorAll(s)];
  const ev = (el, t) => el.dispatchEvent(new w.Event(t, { bubbles: true }));
  const click = el => el.dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  // 模拟拖滑块：改 value 再派发 input（jsdom 没有真实指针拖动）
  const drag = (id, v) => { const el = $('#' + id); el.value = String(v); ev(el, 'input'); };
  const badge = () => $('#bmode').textContent;

  // ---------------- A. 市值区间 ----------------
  ok(!!$('#size_min') && !!$('#size_max'), '存在下限/上限两个滑块');
  ok(!!$('#size_v') && !!$('#size_fill'), '存在读数与填充条');
  ok($$('#size_quick button').length >= 5, '存在市值快捷档位按钮',
    $$('#size_quick button').map(b => b.textContent).join(' / '));

  ok($('#size_min').value === '0' && $('#size_max').value === '2',
    '默认 = 最小30%（smin=0, smax=2）', `${$('#size_min').value},${$('#size_max').value}`);
  ok($('#size_v').textContent === '最小30%', '读数文案 = 最小30%', $('#size_v').textContent);
  ok($$('#size_quick button.on').length === 1 &&
    $$('#size_quick button.on')[0].dataset.smax === '2',
    '「最小30%」快捷按钮被高亮');

  // 3. 拖动不越过：把**被拖的那个把手**夹住（标准 range-slider 行为），
  //    而不是让两个值互换 —— 互换会让手指下的把手突然跳到对面。
  drag('size_min', 5);
  ok($('#size_min').value === '2' && $('#size_max').value === '2',
    '下限拖过上限 → 被夹回上限位置（不越界、不互换）',
    `min=${$('#size_min').value} max=${$('#size_max').value}`);
  ok($('#size_v').textContent === '20%~30%', '重叠后读数 = 单档 20%~30%', $('#size_v').textContent);
  drag('size_max', 8);
  ok($('#size_min').value === '2' && $('#size_max').value === '8',
    '在上限上拖动不受影响：20%~90%', $('#size_v').textContent);
  drag('size_min', 3);
  ok($('#size_max').value === '8' && $('#size_min').value === '3',
    '正常区间：30%~90%', `min=${$('#size_min').value} max=${$('#size_max').value}`);
  ok($('#size_v').textContent === '30%~90%', '读数 = 30%~90%', $('#size_v').textContent);
  drag('size_max', 0);       // 上限拖到下限之下
  ok($('#size_min').value === '3' && $('#size_max').value === '3',
    '上限拖过下限 → 同样被夹住', `min=${$('#size_min').value} max=${$('#size_max').value}`);

  // 4/5. 快捷档位「10%~20%」
  const q = $$('#size_quick button').find(b => b.textContent === '10%~20%');
  ok(!!q, '存在「10%~20%」快捷档位');
  click(q);
  ok($('#size_min').value === '1' && $('#size_max').value === '1',
    '点击后两个滑块都落在第 2 分位（smin=smax=1）',
    `${$('#size_min').value},${$('#size_max').value}`);
  ok($('#size_v').textContent === '10%~20%', '读数 = 10%~20%', $('#size_v').textContent);
  ok(q.classList.contains('on'), '该快捷按钮高亮');

  const cp = w.eval('collect()');
  ok(cp.size_min === 1 && cp.size_max === 1, 'collect() 带出 size_min=1 / size_max=1',
    JSON.stringify({ size_min: cp.size_min, size_max: cp.size_max }));
  ok(cp.size_min !== undefined, 'collect() 一定带 size_min（后端区间口径依赖它）');
  ok(/市值10%~20%/.test(badge()), '徽标显示「市值10%~20%」', badge());

  // 全部 → 不计入条件
  click($$('#size_quick button').find(b => b.textContent === '全部'));
  const cp2 = w.eval('collect()');
  ok(cp2.size_min === 0 && cp2.size_max === 9, '「全部」→ smin=0 / smax=9');
  ok(!/市值/.test(badge()), '全部时徽标不计市值条件', badge());
  ok($('#size_v').textContent === '全部', '读数 = 全部', $('#size_v').textContent);

  // 回到最小30%（后续预设测试用默认值）
  click($$('#size_quick button').find(b => b.textContent === '最小30%'));
  ok($('#size_v').textContent === '最小30%' && /市值最小30%/.test(badge()),
    '回到「最小30%」，徽标同步', badge());

  // ---------------- B. 我的方案 ----------------
  ok(!!$('#preset_name') && !!$('#preset_save') && !!$('#preset_del') && !!$('#preset_stat'),
    '存在名称输入框 / 保存 / 删除 / 状态提示');

  const groups = $$('#preset optgroup').map(g => g.label);
  ok(groups.some(x => /内置/.test(x)), '下拉含「内置方案」分组', groups.join(' | '));
  const fxUsers = F('defaults').user_presets || [];
  ok(fxUsers.length > 0, 'fixture 里有 user_presets（否则本套测试没意义）', String(fxUsers.length));
  ok(groups.some(x => /我的方案/.test(x)), '下拉含「我的方案」分组', groups.join(' | '));
  const u0 = fxUsers[0];
  // ⚠️ fixture 里没有 user_presets 时**不能直接往下走**：u0 是 undefined，
  //    后面每一次 u0.xxx 都会抛 TypeError，整套测试会以「未能解析结果」收场，
  //    看不到真正的原因（上面的 ❌ 断言）。实测栽过一次：
  //    本地 data/user_presets.json 丢了 → fixture 抓成空数组 → 这里直接崩。
  if (!u0) {
    ok('fixture 有 user_presets 才能继续「我的方案」用例', false,
       'fxUsers 为空 —— 检查 data/user_presets.json 是否存在后重跑 make_fixtures.py');
  } else {
    ok($$('#preset option').some(o => o.value === u0.id && o.textContent === u0.name),
      '我的方案已渲染进下拉', `${u0.id} ${u0.name}`);

    // 选中「我的方案」
    $('#preset').value = u0.id;
    ev($('#preset'), 'change');
    await sleep(60);
    ok(!$('#preset_del').disabled, '选中我的方案 → 删除按钮可用');
    ok($('#preset_name').value === u0.name, '名称回填 = 原方案名', $('#preset_name').value);
    const fromUser = w.eval('collect()');
    for (const k of ['px_ma60_min', 'px_ma60_max', 'mkt_state', 'hold']) {
      if (u0.params[k] !== undefined) {
        ok(JSON.stringify(fromUser[k]) === JSON.stringify(u0.params[k]),
          `我的方案的 ${k} 已写回控件`, `${JSON.stringify(fromUser[k])}`);
      }
    }
    ok(/我的方案/.test($('#presetdesc').textContent), '描述区标明是「我的方案」',
      $('#presetdesc').textContent);
    ok(/个条件/.test($('#presetdesc').textContent), '描述区带参数摘要（条件数）',
      $('#presetdesc').textContent);
  }

  // 9. 空名称 → 不发请求
  const before = reqs.length;
  $('#preset_name').value = '   ';
  click($('#preset_save'));
  await sleep(80);
  ok(reqs.length === before, '名称为空时不发保存请求');
  ok(/起个名/.test($('#preset_stat').textContent), '给出红字提示', $('#preset_stat').textContent);
  ok($('#preset_stat').style.color !== '', '提示用醒目颜色');

  // 10/11. 保存
  const before2 = reqs.length;
  $('#preset_name').value = '我的小市值试验';
  // 先把区间归零，再改成「最小20%」——验证存的是**页面上现在的**参数，
  // 而不是「当前选中预设的参数」（用户很可能基于预设又改了两笔）
  click($$('#size_quick button').find(b => b.textContent === '全部'));
  drag('size_max', 1);
  ok($('#size_v').textContent === '最小20%', '改完滑块读数 = 最小20%', $('#size_v').textContent);
  click($('#preset_save'));
  await sleep(150);
  const saveReq = reqs.slice(before2).find(r => r.url.includes('/api/presets/save'));
  ok(!!saveReq, '点击保存 → POST /api/presets/save');
  ok(saveReq && saveReq.method === 'POST', '用 POST 提交');
  ok(saveReq && saveReq.payload.name === '我的小市值试验', 'payload 带名称',
    saveReq && saveReq.payload.name);
  const sp = (saveReq && saveReq.payload.params) || {};
  ok(sp.size_min === 0 && sp.size_max === 1,
    'payload.params 带 size_min / size_max 且等于页面上刚调的区间（0/1 = 最小20%）',
    JSON.stringify({ size_min: sp.size_min, size_max: sp.size_max }));
  ok(sp.px_ma60_min !== undefined && sp.hold !== undefined && Array.isArray(sp.confirm),
    'payload.params 是完整参数（不是只存差异）',
    Object.keys(sp).length + ' 个键');
  ok($$('#preset option').some(o => o.textContent === '我的小市值试验'),
    '保存后新方案出现在下拉里');
  // 选中态用「名称框被回填」来证明 —— 只有选中「我的方案」时才会回填
  ok($('#preset_name').value === '我的小市值试验', '保存后自动选中该方案（名称框回填）',
    `${$('#preset').value} / ${$('#preset_name').value}`);
  ok(!$('#preset_del').disabled, '刚存下的方案可以立刻删除');
  ok(/已保存/.test($('#preset_stat').textContent), '给出保存成功提示', $('#preset_stat').textContent);

  // ---------------- C. 股票池（板块 / 行业 / 自定义代码）也要能存 ----------------
  // 回归：早期保存只发 params，而股票池走独立的 pool 字段 → 板块/行业/自定义代码
  //       整组都没存下来，表现为「保存后切走再切回，板块筛选丢了」。
  const mineId = $('#preset').value;        // 刚保存的那份「我的小市值试验」
  const poolOf = () => w.eval('currentPool()');
  const bdOn = () => $$('#seg-board button.on').map(b => b.dataset.v).sort();
  const setBoards = vs => {
    const s = new Set(vs);
    $$('#seg-board button').forEach(b => b.classList.toggle('on', s.has(b.dataset.v)));
    w.eval('syncBdCnt()');
  };

  // 造一个**明显非默认**的池子
  setBoards(['MAIN', 'BJ']);
  const ibx = $$('#inds input.ibx');
  ok(ibx.length >= 2, '行业树已渲染（fixture industries 可用）', String(ibx.length));
  const pick = [ibx[0].value, ibx[1].value];
  ibx.slice(0, 2).forEach(i => { i.checked = true; ev(i, 'change'); });
  click($$('#seg-pool button').find(b => b.dataset.v === 'custom'));
  $('#pool_codes').value = '000001.SZ, 600519 300750';
  ev($('#pool_codes'), 'input');
  await sleep(30);
  const cur = poolOf();
  ok(cur.mode === 'custom' && cur.boards.length === 2 && cur.industries.length === 2,
    '页面上的池子已是非默认形态',
    JSON.stringify({ mode: cur.mode, boards: cur.boards, inds: cur.industries.length }));

  const beforePool = reqs.length;
  click($('#preset_save'));            // 仍用「我的小市值试验」这个名字 → 覆盖，id 不变
  await sleep(150);
  const poolReq = reqs.slice(beforePool).find(r => r.url.includes('/api/presets/save'));
  ok(!!poolReq && !!poolReq.payload.pool,
    '★ 保存请求带上了 pool 字段（不再只有 params）',
    poolReq ? Object.keys(poolReq.payload).join(',') : '没发出请求');
  const sp2 = (poolReq && poolReq.payload.pool) || {};
  ok(JSON.stringify((sp2.boards || []).slice().sort()) === JSON.stringify(['BJ', 'MAIN']),
    '★ payload.pool.boards = 保存时的板块（主板+北交所）', JSON.stringify(sp2.boards));
  ok((sp2.industries || []).length === 2 && pick.every(x => sp2.industries.includes(x)),
    '★ payload.pool.industries = 保存时勾的 2 个行业', JSON.stringify(sp2.industries));
  ok(sp2.mode === 'custom' && /000001\.SZ/.test(sp2.codes_text || ''),
    '★ payload.pool 带自定义模式与代码清单',
    JSON.stringify({ mode: sp2.mode, codes_text: sp2.codes_text }));
  ok(/股票池/.test($('#preset_stat').textContent), '保存提示里含股票池摘要',
    $('#preset_stat').textContent);

  // 内置预设没有 pool → 切过去**不应篡改**当前股票池（向后兼容）
  const beforeSwitch = poolOf();
  $('#preset').value = F('defaults').presets[0].id;
  ev($('#preset'), 'change');
  await sleep(120);
  const afterSwitch = poolOf();
  ok(JSON.stringify(afterSwitch) === JSON.stringify(beforeSwitch),
    '★ 切到无 pool 的内置预设 → 股票池原样不动（不误伤）',
    JSON.stringify({ mode: afterSwitch.mode, boards: afterSwitch.boards }));

  // 把池子改乱，再切回「我的方案」→ 应完整回填
  setBoards(['MAIN', 'CHINEXT', 'STAR', 'BJ']);
  ibx.slice(0, 2).forEach(i => { i.checked = false; ev(i, 'change'); });
  click($$('#seg-pool button').find(b => b.dataset.v === 'all'));
  $('#pool_codes').value = '';
  ev($('#pool_codes'), 'input');
  await sleep(20);
  ok(poolOf().mode === 'all' && poolOf().boards.length === 4,
    '池子已被改乱（4 个板块全选 / 模式 all）', JSON.stringify(poolOf().boards));

  $('#preset').value = mineId;
  ev($('#preset'), 'change');
  await sleep(150);
  const back = poolOf();
  ok(JSON.stringify(bdOn()) === JSON.stringify(['BJ', 'MAIN']),
    '★★ 切回「我的方案」→ 板块回填（主板+北交所）', JSON.stringify(bdOn()));
  ok(back.industries.length === 2 && pick.every(x => back.industries.includes(x)),
    '★★ 行业回填（2 个）', JSON.stringify(back.industries));
  ok(back.mode === 'custom' && /600519/.test(back.codes_text || ''),
    '★★ 自定义模式与代码清单回填', JSON.stringify({ mode: back.mode, text: back.codes_text }));
  ok($('#poolcustom').style.display === 'block', '自定义代码框重新展开');
  ok(/2/.test($('#bdcnt').textContent), '板块计数同步为「选中 2 个」', $('#bdcnt').textContent);
  ok(/股票池/.test($('#presetdesc').textContent), '描述区显示股票池摘要',
    $('#presetdesc').textContent);

  // 12. 删除
  const newId = $('#preset').value;
  const before3 = reqs.length;
  click($('#preset_del'));
  await sleep(150);
  const delReq = reqs.slice(before3).find(r => r.url.includes('/api/presets/delete'));
  ok(!!delReq && delReq.payload.id === newId, '点击删除 → POST /api/presets/delete 带 id',
    delReq && delReq.payload.id);
  ok(!$$('#preset option').some(o => o.value === newId), '删除后该方案不在下拉里');
  const leftUsers = $$('#preset optgroup').filter(g => /我的方案/.test(g.label));
  ok(leftUsers.length === 1 &&
    leftUsers[0].querySelectorAll('option').length === fxUsers.length,
    '只删掉目标方案，其余「我的方案」原样保留',
    leftUsers.length ? String(leftUsers[0].querySelectorAll('option').length) : '分组都没了');
  ok($('#preset').value === F('defaults').presets[0].id, '回落选中第一个内置方案',
    $('#preset').value);
  ok(/已删除/.test($('#preset_stat').textContent), '给出删除成功提示', $('#preset_stat').textContent);

  // 13. 内置方案不可删
  ok($('#preset_del').disabled, '内置方案时删除按钮禁用');
  const before4 = reqs.filter(r => r.url.includes('/api/presets/delete')).length;
  click($('#preset_del'));
  await sleep(80);
  ok(reqs.filter(r => r.url.includes('/api/presets/delete')).length === before4,
    '内置方案时点删除不发请求');

  // ---------------- D. 界面默认预设（设为默认 / 取消默认） ----------------
  ok(!!$('#preset_def') && !!$('#preset_def_clear'), '「设为默认 / 取消默认」按钮存在');
  // 初始未设置默认 → 取消按钮禁用
  ok($('#preset_def_clear').disabled, '初始（无默认设置）时「取消默认」禁用');
  ok($('#preset_def').textContent === '设为默认',
    '初始时「设为默认」文案正确', $('#preset_def').textContent);

  // 选中第一个内置方案 → 设为默认
  $('#preset').value = F('defaults').presets[0].id;
  $('#preset').dispatchEvent(new w.Event('change'));
  await sleep(80);
  const bid = $('#preset').value;
  const b5 = reqs.length;
  click($('#preset_def'));
  await sleep(150);
  const defReq = reqs.slice(b5).find(r => r.url.includes('/api/presets/set_default'));
  ok(!!defReq && defReq.payload.id === bid,
    '点击「设为默认」→ POST /api/presets/set_default 带当前方案 id',
    defReq && defReq.payload.id);
  ok(/已设为默认/.test($('#preset_stat').textContent), '给出设置成功提示',
    $('#preset_stat').textContent);
  const defOpt = $$('#preset option').find(o => o.value === bid);
  ok(defOpt && /默认/.test(defOpt.textContent), '下拉里该方案带「 · 默认」标记',
    defOpt && defOpt.textContent);
  ok($('#preset_def').disabled && $('#preset_def').textContent === '已是默认方案',
    '当前已是默认 → 按钮禁用并改文案',
    $('#preset_def').textContent + '/' + $('#preset_def').disabled);
  ok(!$('#preset_def_clear').disabled, '有默认设置时「取消默认」可用');

  // 内置方案也可以设为默认（不该被拒绝）
  const otherId = F('defaults').presets[1] ? F('defaults').presets[1].id : null;
  if (otherId) {
    $('#preset').value = otherId;
    $('#preset').dispatchEvent(new w.Event('change'));
    await sleep(80);
    click($('#preset_def'));
    await sleep(150);
    const o2 = $$('#preset option').find(o => o.value === otherId);
    ok(o2 && /默认/.test(o2.textContent), '内置方案也能设为默认', o2 && o2.textContent);
    const o1 = $$('#preset option').find(o => o.value === bid);
    ok(o1 && !/默认/.test(o1.textContent), '旧的默认标记被摘掉（同时只有一个默认）',
      o1 && o1.textContent);
  }

  // 取消默认
  const b6 = reqs.length;
  click($('#preset_def_clear'));
  await sleep(150);
  const clrReq = reqs.slice(b6).find(r => r.url.includes('/api/presets/set_default'));
  ok(!!clrReq && clrReq.payload.id === '',
    '点击「取消默认」→ 发空 id', clrReq && JSON.stringify(clrReq.payload));
  ok(!$$('#preset option').some(o => /默认/.test(o.textContent)),
    '取消后下拉里没有任何「默认」标记');
  ok($('#preset_def_clear').disabled, '取消后「取消默认」重新禁用');
  ok(/取消默认/.test($('#preset_stat').textContent), '给出取消提示',
    $('#preset_stat').textContent);

  // 不存在的 id → 前端本地校验就拦住，不发请求、给出提示
  // （后端也会校验，但前端更早拦掉更省一次往返）
  const b7 = reqs.filter(r => r.url.includes('/api/presets/set_default')).length;
  await w.setDefaultPreset('NOPE');
  await sleep(150);
  ok(/先选一套方案/.test($('#preset_stat').textContent),
    '不存在的 id → 前端本地拦下并提示（不发请求）',
    $('#preset_stat').textContent);
  ok(reqs.filter(r => r.url.includes('/api/presets/set_default')).length === b7,
    '不存在的 id 不发请求');

  // ---------------- E. 默认方案的「股票池」必须被应用（回归） ----------------
  /* ⚠️ 这是一个真实出现过的 bug：
     加了「默认预设」功能后，init() 只调了 applyParams(startP.params)，
     **漏了 applyPool(startP.pool)** —— 而切换预设的那条路径是两个都调的。
     原代码默认用 PRESETS[0]（内置 K 系列，没有 pool），所以一直没暴露；
     一旦把默认换成用户的方案（U3，只选主板），刷新后板块就变成
     defaultPool() 的「主板+科创+创业板」—— 表现为「自动选上了我没选的板块」。

     修法：init() 里在 renderIndustryTree 之后、runAll 之前补 applyPool。
     为什么必须在那之后：applyPool 要勾 `#inds input.ibx`，而行业树那一刻才生成。 */
  {
    const uPool = {
      mode: 'all', codes_text: '600519, 000001.SZ',
      industries: [], boards: ['MAIN'], exchanges: ['SH'],
    };
    const uP = {
      id: 'U9', name: '__pool_probe__', desc: '', saved_at: '',
      params: { ...F('defaults').defaults, size_min: 0, size_max: 3 },
      pool: uPool,
    };
    const b2 = boot({ users: [uP], defaultPreset: 'U9' });
    await sleep(700);
    const d2 = b2.d;
    const $2 = s => d2.querySelector(s), $$2 = s => [...d2.querySelectorAll(s)];

    ok($2('#preset').value === 'U9', '默认预设生效：下拉选中 U9', $2('#preset').value);

    // ★ 核心断言：板块只勾 MAIN
    const bdOn = $$2('#seg-board button').filter(b => b.classList.contains('on'))
      .map(b => b.dataset.v);
    ok(JSON.stringify(bdOn) === JSON.stringify(['MAIN']),
      '★ 默认方案的板块被应用（只勾 MAIN，不是默认的三板块）', JSON.stringify(bdOn));

    // 交易所只勾 SH
    const exOn = $$2('#seg-ex button').filter(b => b.classList.contains('on'))
      .map(b => b.dataset.v);
    ok(JSON.stringify(exOn) === JSON.stringify(['SH']),
      '默认方案的交易所被应用（只勾 SH）', JSON.stringify(exOn));

    // 自定义代码回填
    ok($2('#pool_codes').value.includes('600519'),
      '默认方案的自定义代码被回填', $2('#pool_codes').value);

    // 反向：没有 pool 的方案 → 保持**默认池**（不是保留上次的勾选，也不是清空）
    // ⚠️ 期望值跟随 defaultPool()，不写死板块清单（默认池可配置）
    const b3 = boot({ users: [{ id: 'U8', name: '__nopool__', desc: '', saved_at: '',
      params: { ...F('defaults').defaults }, pool: null }], defaultPreset: 'U8' });
    await sleep(700);
    const bdOn3 = [...b3.d.querySelectorAll('#seg-board button')]
      .filter(b => b.classList.contains('on')).map(b => b.dataset.v);
    const dflt = b3.w.defaultPool().boards;
    ok(JSON.stringify(bdOn3) === JSON.stringify(dflt),
      '无 pool 的方案 → 板块回落到默认池（不被清空）',
      `${JSON.stringify(bdOn3)} vs 默认 ${JSON.stringify(dflt)}`);
  }

  // ---------------- C. 运行时错误 ----------------
  ok(errors.length === 0, '无运行时错误', errors.slice(0, 3).join(' | '));

  console.log('');
  console.log(`✅  市值区间 + 我的方案 + 股票池：${pass}/${pass + fail} 通过`);
  process.exit(fail ? 1 : 0);
})();
