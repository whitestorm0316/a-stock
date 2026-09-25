/* 视觉核对：截「容量约束」整组（含「建仓节奏·阶梯建仓」），
 * 并把节奏切到指定序列，让柱状示意与提示都渲染出来。
 *
 * 用法: node tests/shot_ladder.js [输出png] [序列]   # 默认 output/_shot/ladder.png 与 1,2,2,2,3
 *   例: node tests/shot_ladder.js output/_shot/ladder_112233.png 1,1,2,2,3,3
 * 序列若不是内置预设，会自动走「自定义…」并填进输入框。
 * 需要一个已在运行的 app/server.py（默认 8770）与本机 Chrome。
 * 与 shot_ui.js 一样只用于人工视觉核对，不参与 CI。
 */
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const http = require('http');

const OUT = process.argv[2] || path.join(__dirname, '..', 'output', '_shot', 'ladder.png');
const LAD = process.argv[3] || '1,2,2,2,3';
const CHROME = [
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
  path.join(os.homedir(), 'AppData/Local/Google/Chrome/Application/chrome.exe'),
].find(p => fs.existsSync(p));
if (!CHROME) { console.error('找不到 Chrome'); process.exit(2); }

const PORT = 9466;
const PROFILE = fs.mkdtempSync(path.join(os.tmpdir(), 'shotlad-'));
const get = url => new Promise((res, rej) => {
  http.get(url, r => { let b = ''; r.on('data', d => b += d); r.on('end', () => res(JSON.parse(b))); })
    .on('error', rej);
});

(async () => {
  const chrome = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--no-proxy-server',
    `--remote-debugging-port=${PORT}`, `--user-data-dir=${PROFILE}`,
    '--window-size=1500,1200', 'about:blank',
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
  await S('Page.navigate', { url: 'http://127.0.0.1:8770/' });

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
  await waitFor('!!document.querySelector("#cap_rhythm") && document.querySelector("#cap_rhythm").options.length > 2');

  // 打开「限制持仓」口径 + 选中指定序列，触发渲染
  const info = await S('Runtime.evaluate', {
    returnByValue: true,
    expression: `(() => {
      const want = ${JSON.stringify(LAD)};
      const on = document.querySelector('#seg-cap button[data-v="1"]');
      if (on) on.click();
      const sel = document.querySelector('#cap_rhythm');
      const has = [...sel.options].some(o => o.value === want);
      if (has) {
        sel.value = want;
        sel.dispatchEvent(new Event('change', { bubbles: true }));
      } else {
        sel.value = '__custom__';
        sel.dispatchEvent(new Event('change', { bubbles: true }));
        const inp = document.querySelector('#cap_ladder');
        inp.value = want;
        inp.dispatchEvent(new Event('input', { bubbles: true }));
      }
      // 左栏滚到容量约束组
      const wrap = document.querySelector('#capwrap');
      const aside = wrap.closest('aside');
      aside.scrollTop += wrap.getBoundingClientRect().top - 70;
      const r = wrap.getBoundingClientRect();
      return {
        badge: (document.querySelector('#cap_badge') || {}).textContent,
        rhythm: sel.value,
        ladder_inp: (document.querySelector('#cap_ladder') || {}).value,
        maxpos: (document.querySelector('#cap_maxpos') || {}).value,
        maxpos_disabled: (document.querySelector('#cap_maxpos') || {}).disabled,
        maxnew_disabled: (document.querySelector('#cap_maxnew') || {}).disabled,
        info_txt: (document.querySelector('#cap_ladder_info') || {}).textContent,
        bars: [...document.querySelectorAll('#cap_ladder_info .ladbar em')].map(e => e.textContent),
        x: r.left, y: r.top, w: r.width, h: r.height,
      };
    })()`,
  });
  const v = info.result.value;
  console.log('状态:', JSON.stringify(v, null, 0));
  await new Promise(r => setTimeout(r, 600));

  const shot = await S('Page.captureScreenshot', {
    format: 'png',
    clip: { x: Math.max(0, v.x - 6), y: Math.max(0, v.y - 6),
            width: Math.ceil(v.w + 12), height: Math.ceil(v.h + 12), scale: 2 },
  });
  fs.mkdirSync(path.dirname(OUT), { recursive: true });
  fs.writeFileSync(OUT, Buffer.from(shot.data, 'base64'));
  console.log('已写出', OUT);
  ws.close(); chrome.kill(); process.exit(0);
})().catch(e => { console.error('失败:', e.message); process.exit(1); });
