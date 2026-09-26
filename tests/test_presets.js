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
function boot() {
  const FX = {
    meta: F('meta'), defaults: F('defaults'),
    backtest: F('backtest'), scan: F('scan'),
  };
  let users = (FX.defaults.user_presets || []).slice();
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
          body = { ...FX.defaults, user_presets: users };
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

  // ---------------- C. 运行时错误 ----------------
  ok(errors.length === 0, '无运行时错误', errors.slice(0, 3).join(' | '));

  console.log('');
  console.log(`✅  市值区间 + 我的方案 + 股票池：${pass}/${pass + fail} 通过`);
  process.exit(fail ? 1 : 0);
})();
