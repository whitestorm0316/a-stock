/* 前端视觉核对：用真实 Chrome（CDP）打开页面并截图。
 *
 * 用法：
 *   node tests/shot_ui.js <输出png> [门类代码] [top|bottom]
 *     node tests/shot_ui.js output/_shot/ind.png C          # 展开门类 C 后截图
 *     node tests/shot_ui.js output/_shot/bottom.png A bottom # 收起全部、滚到门类清单底部
 *     node tests/shot_ui.js output/_shot/top.png C top       # 只截顶部标题栏（核对图标）
 *
 * 需要一个已在运行的 app/server.py（默认 8770）与本机 Chrome。
 * 仅用于人工视觉核对，不参与 CI —— jsdom 能验 DOM 与事件，但渲染不出布局。
 */
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const http = require('http');

const OUT = process.argv[2] || path.join(__dirname, '..', 'output', '_shot', 'ind.png');
const OPEN_CODE = process.argv[3] || 'C';
const CHROME = [
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
  path.join(os.homedir(), 'AppData/Local/Google/Chrome/Application/chrome.exe'),
].find(p => fs.existsSync(p));
if (!CHROME) { console.error('找不到 Chrome'); process.exit(2); }

const PORT = 9455;
const PROFILE = fs.mkdtempSync(path.join(os.tmpdir(), 'shot-'));

const get = url => new Promise((res, rej) => {
  http.get(url, r => { let b = ''; r.on('data', d => b += d); r.on('end', () => res(JSON.parse(b))); })
    .on('error', rej);
});

function cdp(ws) {
  let id = 0;
  const pend = new Map();
  ws.addEventListener('message', e => {
    const m = JSON.parse(e.data);
    if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); }
  });
  return (method, params) => new Promise((res, rej) => {
    const i = ++id;
    pend.set(i, m => m.error ? rej(new Error(method + ': ' + m.error.message)) : res(m.result));
    ws.send(JSON.stringify({ id: i, method, params: params || {} }));
  });
}

(async () => {
  const chrome = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--no-proxy-server',
    `--remote-debugging-port=${PORT}`, `--user-data-dir=${PROFILE}`,
    '--window-size=1500,1100', 'about:blank',
  ], { stdio: 'ignore' });

  // 等 CDP 起来
  let ver = null;
  for (let i = 0; i < 60 && !ver; i++) {
    try { ver = await get(`http://127.0.0.1:${PORT}/json/version`); }
    catch (e) { await new Promise(r => setTimeout(r, 300)); }
  }
  if (!ver) { console.error('CDP 未就绪'); chrome.kill(); process.exit(3); }

  // Node 22 自带全局 WebSocket（undici），无需额外依赖
  const ws = new WebSocket(ver.webSocketDebuggerUrl);
  await new Promise((res, rej) => {
    ws.addEventListener('open', res, { once: true });
    ws.addEventListener('error', e => rej(new Error('WS 连接失败')), { once: true });
  });
  const send = cdp(ws);

  const { targetId } = await send('Target.createTarget', { url: 'about:blank' });
  const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true });
  let msgId = 0;
  const S = (m, p) => new Promise((res, rej) => {
    const id = ++msgId;
    const h = ev => {
      const d = JSON.parse(ev.data);
      if (d.id === id) {
        ws.removeEventListener('message', h);
        d.error ? rej(new Error(d.error.message)) : res(d.result);
      }
    };
    ws.addEventListener('message', h);
    ws.send(JSON.stringify({ id, method: m, params: p || {}, sessionId }));
  });

  await S('Page.enable');
  await S('Runtime.enable');
  await S('Page.navigate', { url: 'http://127.0.0.1:8770/' });

  // 等行业树渲染出来
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

  // 模式 top：只截顶部标题栏（核对 .brand 图标 / 标题排版），不碰行业树
  if (process.argv[4] === 'top') {
    const m = await S('Page.getLayoutMetrics');
    const shot = await S('Page.captureScreenshot', {
      format: 'png',
      clip: { x: 0, y: 0, width: Math.min(1400, m.cssLayoutViewport.clientWidth),
              height: 58, scale: 2 },
    });
    fs.mkdirSync(path.dirname(OUT), { recursive: true });
    fs.writeFileSync(OUT, Buffer.from(shot.data, 'base64'));
    console.log('已写出（顶部标题栏）', OUT);
    ws.close();
    chrome.kill();
    process.exit(0);
  }

  // 展开目标门类，并把左栏滚到行业筛选处
  const code = JSON.stringify(OPEN_CODE);
  const SCROLL_BOTTOM = process.argv[4] === 'bottom';
  const r = await S('Runtime.evaluate', {
    returnByValue: true,
    expression: `(() => {
      const g = [...document.querySelectorAll('#inds .igt')].find(x => x.dataset.code === ${code});
      if (!g) return 'no-group';
      if (${SCROLL_BOTTOM}) {
        // 全部收起，只看门类清单 + 末尾的「无细分行业」「未归类」
        document.querySelectorAll('#inds .igt').forEach(x => x.classList.remove('indopen'));
      } else {
        g.classList.add('indopen');
      }
      const box = document.querySelector('#inds');
      const aside = box.closest('aside');
      const lbl = box.previousElementSibling;
      if (aside) aside.scrollTop = aside.scrollTop + lbl.getBoundingClientRect().top - 60;
      if (${SCROLL_BOTTOM}) box.scrollTop = box.scrollHeight;
      return 'ok:' + g.querySelector('.ignm').textContent.trim()
                + ' subs=' + g.querySelectorAll('input.ibx').length
                + ' groups=' + document.querySelectorAll('#inds .igt').length
                + ' absent=' + document.querySelectorAll('#inds .iga').length;
    })()`,
  });
  console.log('展开结果:', r.result.value);
  await new Promise(r2 => setTimeout(r2, 500));

  const shot = await S('Page.captureScreenshot', { format: 'png' });
  fs.mkdirSync(path.dirname(OUT), { recursive: true });
  fs.writeFileSync(OUT, Buffer.from(shot.data, 'base64'));
  console.log('已写出', OUT);

  ws.close();
  chrome.kill();
  process.exit(0);
})().catch(e => { console.error('失败:', e.message); process.exit(1); });
