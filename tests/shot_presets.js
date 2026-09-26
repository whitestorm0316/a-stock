/* 视觉核对：截左栏三组新 UI ——
 *   ①「预设方案」组（下拉分组 + 名称框 + 保存/删除 + 状态行）
 *   ②「市值区间」组（双把手滑块 + 档位快捷按钮 + 读数）
 *   ③「板块」组（股票池回填效果：走真实接口存一份带 pool 的方案，
 *      刷新后选中，看板块是否被正确复位；截完即删）
 *
 * 用法: node tests/shot_presets.js [输出目录] [档位文案]
 *   默认 output/_shot/ + 「10%~30%」（两个把手分开的形态）。
 *   换 [档位文案] 可看别的档位，例如单档重叠的形态：
 *     node tests/shot_presets.js output/_shot "10%~20%"
 *   产出 presets_panel.png / size_band.png / pool_saved.png 三张。
 * 需要一个已在运行的 app/server.py（默认 8770）与本机 Chrome。
 * 与 shot_ui.js / shot_ladder.js 一样只用于人工视觉核对，不参与 CI。
 * ⚠️ 会临时往 data/user_presets.json 写一份 __shot_pool__ 方案，结束前自动删除。
 */
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const http = require('http');

const OUTDIR = process.argv[2] || path.join(__dirname, '..', 'output', '_shot');
const BAND = process.argv[3] || '10%~30%';
const PORT_APP = 8770;
const CHROME = [
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
  path.join(os.homedir(), 'AppData/Local/Google/Chrome/Application/chrome.exe'),
].find(p => fs.existsSync(p));
if (!CHROME) { console.error('找不到 Chrome'); process.exit(2); }

const PORT = 9467;
const PROFILE = fs.mkdtempSync(path.join(os.tmpdir(), 'shotpre-'));
const get = url => new Promise((res, rej) => {
  http.get(url, r => { let b = ''; r.on('data', d => b += d); r.on('end', () => res(JSON.parse(b))); })
    .on('error', rej);
});

(async () => {
  const chrome = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--no-proxy-server',
    `--remote-debugging-port=${PORT}`, `--user-data-dir=${PROFILE}`,
    '--window-size=1500,1400', 'about:blank',
  ], { stdio: 'ignore' });

  let ver = null;
  for (let i = 0; i < 60 && !ver; i++) {
    try { ver = await get(`http://127.0.0.1:${PORT}/json/version`); }
    catch (e) { await new Promise(r => setTimeout(r, 300)); }
  }
  if (!ver) { console.error('CDP 未就绪'); chrome.kill(); process.exit(3); }

  const ws = new WebSocket(ver.webSocketDebuggerUrl);
  await new Promise((res, rej) => {
    ws.addEventListener('open', res, { once: true });
    ws.addEventListener('error', () => rej(new Error('WS 连接失败')), { once: true });
  });
  let msgId = 0; const pend = new Map();
  ws.addEventListener('message', ev => {
    const d = JSON.parse(ev.data);
    if (d.id && pend.has(d.id)) { pend.get(d.id)(d); pend.delete(d.id); }
  });
  const raw = (m, p) => new Promise((res, rej) => {
    const id = ++msgId;
    pend.set(id, d => d.error ? rej(new Error(m + ': ' + d.error.message)) : res(d.result));
    ws.send(JSON.stringify({ id, method: m, params: p || {} }));
  });

  const { targetId } = await raw('Target.createTarget', { url: 'about:blank' });
  const { sessionId } = await raw('Target.attachToTarget', { targetId, flatten: true });
  let sid = 0; const pend2 = new Map();
  ws.addEventListener('message', ev => {
    const d = JSON.parse(ev.data);
    if (d.id && pend2.has(d.id)) { pend2.get(d.id)(d); pend2.delete(d.id); }
  });
  const S = (m, p) => new Promise((res, rej) => {
    const id = ++sid;
    pend2.set(id, d => d.error ? rej(new Error(m + ': ' + d.error.message)) : res(d.result));
    ws.send(JSON.stringify({ id, method: m, params: p || {}, sessionId }));
  });

  await S('Page.enable');
  await S('Runtime.enable');
  await S('Page.navigate', { url: `http://127.0.0.1:${PORT_APP}/` });

  const waitFor = async (expr, ms = 120000) => {
    const t0 = Date.now();
    while (Date.now() - t0 < ms) {
      const r = await S('Runtime.evaluate', { expression: expr, returnByValue: true });
      if (r.result && r.result.value) return true;
      await new Promise(r2 => setTimeout(r2, 400));
    }
    throw new Error('等待超时: ' + expr);
  };
  await waitFor('!!document.querySelector("#inds .igt")');
  await waitFor('!!document.querySelector("#size_dual") && document.querySelector("#size_min")');

  // ① 把市值区间切到「10%~20%」，并选中一份「我的方案」看保存区的完整形态
  const st = await S('Runtime.evaluate', {
    returnByValue: true,
    expression: `(() => {
      // ⚠️ 顺序要紧：applyParams 会把选中预设的市值区间写回滑块，
      //    所以必须**先**选预设、**后**点档位按钮，否则点完就被覆盖回去。
      const sel = document.querySelector('#preset');
      const mine = [...sel.options].find(o =>
        (o.parentNode.label || '').indexOf('我的方案') >= 0);
      if (mine) {
        sel.value = mine.value;
        sel.dispatchEvent(new Event('change', { bubbles: true }));
      }
      const q = [...document.querySelectorAll('#size_quick button')]
        .find(b => b.textContent === ${JSON.stringify(BAND)});
      if (q) q.click();
      const aside = document.querySelector('#side');
      const g = sel.closest('.grp');
      aside.scrollTop += g.getBoundingClientRect().top - 66;
      const gp = g.getBoundingClientRect();
      const gs = document.querySelector('#size_dual').closest('.grp').getBoundingClientRect();
      return {
        preset: sel.value,
        name: (document.querySelector('#preset_name') || {}).value,
        del_disabled: (document.querySelector('#preset_del') || {}).disabled,
        desc: (document.querySelector('#presetdesc') || {}).textContent,
        size_readout: (document.querySelector('#size_v') || {}).textContent,
        smin: (document.querySelector('#size_min') || {}).value,
        smax: (document.querySelector('#size_max') || {}).value,
        px: gp.left, py: gp.top, pw: gp.width, ph: gp.height,
        sy: gs.top, sh: gs.height,
      };
    })()`,
  });
  const v = st.result.value;
  console.log('状态:', JSON.stringify(v, null, 0));
  await new Promise(r => setTimeout(r, 700));

  fs.mkdirSync(OUTDIR, { recursive: true });
  const shoot = async (name, clip) => {
    const shot = await S('Page.captureScreenshot', {
      format: 'png',
      clip: { ...clip, scale: 2 },
    });
    const p = path.join(OUTDIR, name);
    fs.writeFileSync(p, Buffer.from(shot.data, 'base64'));
    console.log('已写出', p);
  };

  // ① 预设方案组
  await shoot('presets_panel.png', {
    x: Math.max(0, v.px - 6), y: Math.max(0, v.py - 6),
    width: Math.ceil(v.pw + 12), height: Math.ceil(v.ph + 12),
  });
  // ② 市值区间组（视口内位置可能变过，重新取一次）
  const box = await S('Runtime.evaluate', {
    returnByValue: true,
    expression: `(() => {
      const r = document.querySelector('#size_dual').closest('.grp').getBoundingClientRect();
      return { x: r.left, y: r.top, w: r.width, h: r.height };
    })()`,
  });
  const b = box.result.value;
  await shoot('size_band.png', {
    x: Math.max(0, b.x - 6), y: Math.max(0, b.y - 6),
    width: Math.ceil(b.w + 12), height: Math.ceil(b.h + 12),
  });

  // ③ 股票池随方案保存 / 回填
  //    回归现场：早期 /api/presets/save 只收 params，pool 整组丢失，
  //    表现为「保存后切走再切回，板块筛选没了」。这里走**真实接口**存一份，
  //    刷新后选中它，看板块是否被正确回填；截完即删，不留垃圾数据。
  const PNAME = '__shot_pool__';
  const mk = await S('Runtime.evaluate', {
    awaitPromise: true, returnByValue: true,
    expression: `(async () => {
      const r = await fetch('/api/presets/save', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: ${JSON.stringify(PNAME)},
          params: { px_ma60_min: 1, px_ma60_max: 1, size_min: 0, size_max: 2,
                    mkt_state: 'bear', hold: 20 },
          pool: { mode: 'custom', codes_text: '600519, 000001.SZ',
                  industries: ['半导体', '中药'],
                  boards: ['MAIN'], exchanges: ['SH'] },
        }),
      });
      return await r.json();
    })()`,
  });
  console.log('建方案:', JSON.stringify(mk.result.value && mk.result.value.msg));

  await S('Page.navigate', { url: `http://127.0.0.1:${PORT_APP}/` });
  await waitFor('!!document.querySelector("#inds .igt")');
  await waitFor('!!document.querySelector("#size_dual")');
  const back = await S('Runtime.evaluate', {
    returnByValue: true,
    expression: `(() => {
      const sel = document.querySelector('#preset');
      const opt = [...sel.options].find(o => o.textContent === ${JSON.stringify(PNAME)});
      if (!opt) return { err: '下拉里没有该方案' };
      sel.value = opt.value;
      sel.dispatchEvent(new Event('change', { bubbles: true }));
      return {
        boards_on: [...document.querySelectorAll('#seg-board button.on')]
          .map(b => b.dataset.v),
        ex_on: [...document.querySelectorAll('#seg-ex button.on')].map(b => b.dataset.v),
        modes: [...document.querySelectorAll('#seg-pool button.on')]
          .map(b => b.dataset.v),
        codes: (document.querySelector('#pool_codes') || {}).value,
        codes_box: (document.querySelector('#poolcustom') || {}).style.display,
        n_inds: document.querySelectorAll('#inds input.ibx:checked').length,
        desc: (document.querySelector('#presetdesc') || {}).textContent,
      };
    })()`,
  });
  const bv = back.result.value;
  console.log('回填:', JSON.stringify(bv));
  await new Promise(r => setTimeout(r, 700));

  // 截「板块」这一小段（用户报的就是它丢了）
  // ⚠️ 板块组在左栏很靠下，必须先滚进视口，否则 getBoundingClientRect 给的
  //    视口坐标在可视区外 → clip 截出来是全白。
  await S('Runtime.evaluate', {
    returnByValue: true,
    expression: `(() => {
      const s = document.querySelector('#seg-board');
      const aside = s.closest('aside') || document.querySelector('#side');
      if (aside) aside.scrollTop += s.getBoundingClientRect().top - 180;
      return true;
    })()`,
  });
  await new Promise(r => setTimeout(r, 500));
  const sb = await S('Runtime.evaluate', {
    returnByValue: true,
    expression: `(() => {
      const s = document.querySelector('#seg-board');
      const cap = s.nextElementSibling;
      const r1 = s.getBoundingClientRect(), r2 = cap.getBoundingClientRect();
      const top = r1.top - 34;                    // 往上多取一点，带上「板块」标题
      return { x: Math.min(r1.left, r2.left) - 6, y: Math.max(0, top - 6),
               w: Math.max(r1.right, r2.right) - Math.min(r1.left, r2.left) + 12,
               h: r2.bottom - top + 12, in_view: r1.top > 0 && r2.bottom < innerHeight };
    })()`,
  });
  const s2 = sb.result.value;
  if (!s2.in_view) console.warn('⚠️ 板块组仍在视口外，截图可能不全:', JSON.stringify(s2));
  await shoot('pool_saved.png', { x: s2.x, y: s2.y, width: Math.ceil(s2.w), height: Math.ceil(s2.h) });

  // 收拾干净：删掉这份演示方案
  const rm = await S('Runtime.evaluate', {
    awaitPromise: true, returnByValue: true,
    expression: `(async () => {
      const d = await (await fetch('/api/defaults')).json();
      const h = (d.user_presets || []).find(x => x.name === ${JSON.stringify(PNAME)});
      if (!h) return null;
      await fetch('/api/presets/delete', { method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: h.id }) });
      return h.id;
    })()`,
  });
  console.log('已清理演示方案:', rm.result.value || '（没找到）');

  ws.close(); chrome.kill(); process.exit(0);
})().catch(e => { console.error('失败:', e.message); process.exit(1); });
