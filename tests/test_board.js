/* 前端「板块筛选」验证（jsdom 无头）
 *
 * 覆盖：
 *   1. seg-board 存在且为多选（默认 主板/创业板/科创板 选中，北交所不选）
 *   2. 点击切换是 toggle 而非单选
 *   3. 全选 / 清空 按钮只作用于板块，不影响行业复选框
 *   4. currentPool() 把 boards 正确带入 /api/scan 请求体
 *   5. 数量徽章来自 meta.board_counts
 *   6. 清空后请求体 boards 为空数组（后端语义 = 不限）
 *   7. 重置按钮恢复默认板块
 *   8. 无运行时错误
 */
const fs = require('fs');
// jsdom 从项目根 node_modules 解析（见下方 ROOT）

const path = require('path');
const ROOT = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'app/index.html'), 'utf8');
const { JSDOM } = require(require('path').join(ROOT, 'node_modules/jsdom'));
const F = n => JSON.parse(fs.readFileSync(path.join(__dirname, `fixtures/fixture_${n}.json`), 'utf8'));
const FX = { meta: F('meta'), defaults: F('defaults'), backtest: F('backtest'),
             scan: F('scan'), trades: F('trades') };

const errors = [], warnLog = [];
const reqLog = [];   // { url, body }

const dom = new JSDOM(html, {
  runScripts: 'dangerously',
  pretendToBeVisual: true,
  url: 'http://127.0.0.1:8771/',
  beforeParse(w) {
    w.HTMLElement.prototype.scrollIntoView = () => {};
    w.requestAnimationFrame = cb => setTimeout(() => cb(Date.now()), 0);
    w.echarts = { init: () => ({ setOption() {}, resize() {}, dispose() {}, getDom: () => null }) };
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
    w.console.warn = (...a) => warnLog.push(a.join(' '));
  },
});

const w = dom.window, d = w.document;
const $ = s => d.querySelector(s), $$ = s => [...d.querySelectorAll(s)];
const click = el => el.dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
const sleep = ms => new Promise(r => setTimeout(r, ms));
/* 取最近一次 /api/scan 请求体里的 pool.boards */
const lastBoards = () => {
  const hit = [...reqLog].reverse().find(x => x.url.includes('/api/scan'));
  return hit ? (hit.body.pool || {}).boards : undefined;
};

(async () => {
  await sleep(2800);
  const out = [];
  const ok = (t, c, x) => out.push(`${c ? '✅' : '❌'} ${t}${x !== undefined ? '  → ' + x : ''}`);

  // ---- 1. UI 存在性与初始状态
  const seg = $('#seg-board');
  ok('板块筛选控件存在', !!seg);
  if (!seg) { report(); return; }
  const btns = $$('#seg-board button');
  ok('板块 4 个按钮', btns.length === 4, '实际 ' + btns.length);
  const vals = btns.map(b => b.dataset.v);
  ok('取值 = MAIN/CHINEXT/STAR/BJ',
     JSON.stringify(vals) === JSON.stringify(['MAIN', 'CHINEXT', 'STAR', 'BJ']), vals.join(','));
  const labels = btns.map(b => b.textContent.replace(/[\d,]/g, '').trim());
  ok('标签为中文', labels.join(',') === '主板,创业板,科创板,北交所', labels.join(','));
  ok('默认选中 主板/创业板/科创板',
     btns.filter(b => b.classList.contains('on')).map(b => b.dataset.v).join(',') === 'MAIN,CHINEXT,STAR',
     btns.filter(b => b.classList.contains('on')).map(b => b.dataset.v).join(','));
  ok('默认不选北交所', !btns.find(b => b.dataset.v === 'BJ').classList.contains('on'));

  // ---- 2. 数量徽章来自 meta.board_counts
  const bc = FX.meta.board_counts || {};
  const badgeTxt = ['MAIN', 'CHINEXT', 'STAR'].map(k => $('#sbc-' + k).textContent.trim());
  ok('主板徽章有数字', /\d/.test(badgeTxt[0]), badgeTxt[0]);
  ok('创业板徽章有数字', /\d/.test(badgeTxt[1]), badgeTxt[1]);
  ok('科创板徽章有数字', /\d/.test(badgeTxt[2]), badgeTxt[2]);
  const expectMain = new Intl.NumberFormat('zh-CN').format(Math.round(bc.MAIN));
  ok('主板徽章与 meta 一致', badgeTxt[0] === expectMain, `${badgeTxt[0]} vs ${expectMain}`);

  // ---- 3. 计数提示
  ok('计数提示初始为「选中 3 个」', /选中 3 个/.test($('#bdcnt').textContent),
     $('#bdcnt').textContent);

  // ---- 4. 点击是 toggle（多选），不是单选
  click(btns.find(b => b.dataset.v === 'STAR'));
  await sleep(50);
  ok('取消科创板后仍保留主板', btns.find(b => b.dataset.v === 'MAIN').classList.contains('on'));
  ok('取消科创板后计数=2', /选中 2 个/.test($('#bdcnt').textContent), $('#bdcnt').textContent);
  click(btns.find(b => b.dataset.v === 'BJ'));
  await sleep(50);
  ok('点击北交所可选中（多选而非单选）', btns.find(b => b.dataset.v === 'BJ').classList.contains('on'));
  ok('加入北交所后计数=3', /选中 3 个/.test($('#bdcnt').textContent), $('#bdcnt').textContent);

  // ---- 5. 请求体携带 boards
  const before = reqLog.length;
  click($('#run'));
  await sleep(700);
  ok('点运行触发请求', reqLog.length > before, '新增 ' + (reqLog.length - before) + ' 次');
  const reqBds = lastBoards();
  ok('请求体含 pool.boards', Array.isArray(reqBds), JSON.stringify(reqBds));
  // 此时勾选状态：MAIN 保留、CHINEXT 保留、STAR 被取消、BJ 被加入
  ok('boards 内容 = 当前勾选', JSON.stringify(reqBds) === JSON.stringify(['MAIN', 'CHINEXT', 'BJ']),
     JSON.stringify(reqBds));

  // ---- 6. 全选 / 清空（只作用于板块，不动行业）
  // 先勾一个行业，验证板块操作不误伤它
  const firstInd = $('#inds input');
  firstInd.checked = true;
  click($$('.poolcls button').find(b => b.dataset.act === 'allind' ? false : b.dataset.act === 'allbd'));
  await sleep(60);
  ok('全选后 4 个板块全选', btns.every(b => b.classList.contains('on')),
     btns.filter(b => b.classList.contains('on')).length + '/4');
  ok('全选未误动行业复选框', firstInd.checked === true);

  click($$('.poolcls button').find(b => b.dataset.act === 'nonebd'));
  await sleep(60);
  ok('清空后 0 个板块选中', btns.every(b => !b.classList.contains('on')),
     btns.filter(b => b.classList.contains('on')).length + '/4');
  ok('清空未误动行业复选框', firstInd.checked === true);
  ok('清空后提示 0 选中 = 不限', /0 选中 = 不限/.test($('#bdcnt').textContent), $('#bdcnt').textContent);

  // ---- 7. 清空 = 不限：请求体 boards 为空数组
  const b2 = reqLog.length;
  click($('#run'));
  await sleep(700);
  ok('清空后请求 boards = []', JSON.stringify(lastBoards()) === '[]', JSON.stringify(lastBoards()));

  // ---- 8. 行业「全选」不能误动板块（反向校验）
  click($$('.poolcls button').find(b => b.dataset.act === 'allind'));
  await sleep(60);
  ok('行业全选后板块仍为 0', btns.every(b => !b.classList.contains('on')),
     btns.filter(b => b.classList.contains('on')).length + '/4');

  // ---- 9. 重置恢复默认
  click($('#reset'));
  await sleep(700);
  ok('重置后恢复 主板/创业板/科创板',
     btns.filter(b => b.classList.contains('on')).map(b => b.dataset.v).join(',') === 'MAIN,CHINEXT,STAR',
     btns.filter(b => b.classList.contains('on')).map(b => b.dataset.v).join(','));
  ok('重置后北交所仍不选', !btns.find(b => b.dataset.v === 'BJ').classList.contains('on'));
  ok('重置后请求 boards 已恢复', JSON.stringify(lastBoards()) === JSON.stringify(['MAIN', 'CHINEXT', 'STAR']),
     JSON.stringify(lastBoards()));
  ok('重置后行业恢复全不选', $$('#inds input:checked').length === 0);

  // ---- 10. 交易所与板块并存
  ok('交易所控件仍在', !!$('#seg-ex') && $$('#seg-ex button').length === 3);
  const exReq = [...reqLog].reverse().find(x => x.url.includes('/api/scan'));
  ok('请求体同时含 boards 与 exchanges',
     Array.isArray(exReq.body.pool.boards) && Array.isArray(exReq.body.pool.exchanges),
     `boards=${JSON.stringify(exReq.body.pool.boards)} exchanges=${JSON.stringify(exReq.body.pool.exchanges)}`);

  ok('无运行时错误', errors.length === 0, errors.slice(0, 3).join(' | ') || '干净');

  report();

  function report() {
    console.log('\n══════ 前端「板块筛选」验证 ══════\n');
    out.forEach(l => console.log(l));
    const pass = out.filter(l => l.startsWith('✅')).length;
    console.log(`\n通过 ${pass}/${out.length}`);
    if (warnLog.length) console.log('警告:', warnLog.slice(0, 4).join(' | '));
    process.exit(0);
  }
})();
