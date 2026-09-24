/* 真实 Chrome（CDP）验证「开启仓位约束 → 运行回测 → 结果渲染」
 *
 * 用 node 内置 WebSocket 连 Chrome DevTools Protocol，避免装 puppeteer。
 * 覆盖 jsdom 覆盖不到的部分：真实 ECharts 渲染、真实 fetch、真实布局。
 *
 * ⚠️ 关键教训（第一版脚本就栽在这）：
 *   init() 里 runAll() 会先跑一次「不限仓位」回测，耗时约 14s。
 *   在此期间 #run 按钮是 disabled，点击会被忽略 —— 必须**等按钮可用**再操作，
 *   否则拿到的仍是旧口径结果，后半段断言全部误判为「渲染失败」。
 */
const URL_APP = 'http://127.0.0.1:8772/';
const CDP = 'http://127.0.0.1:9333';

let id = 0;
function rpc(ws, method, params) {
  return new Promise((res, rej) => {
    const mid = ++id;
    const on = ev => {
      const m = JSON.parse(ev.data);
      if (m.id === mid) {
        ws.removeEventListener('message', on);
        m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result);
      }
    };
    ws.addEventListener('message', on);
    ws.send(JSON.stringify({ id: mid, method, params }));
  });
}

async function evalJS(ws, expr, awaitPromise = true) {
  const r = await rpc(ws, 'Runtime.evaluate', {
    expression: expr, awaitPromise, returnByValue: true
  });
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.text + ' :: ' +
    (r.exceptionDetails.exception && r.exceptionDetails.exception.description));
  return r.result.value;
}

const sleep = ms => new Promise(r => setTimeout(r, ms));
const strip = s => String(s).replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ');

(async () => {
  const t = await (await fetch(`${CDP}/json/new?${encodeURIComponent(URL_APP)}`,
    { method: 'PUT' })).json();
  const ws = new WebSocket(t.webSocketDebuggerUrl);
  await new Promise(r => ws.addEventListener('open', r, { once: true }));
  await rpc(ws, 'Runtime.enable');
  await rpc(ws, 'Page.enable');
  // 视口放大，避免窄屏布局把图表压成 100px
  await rpc(ws, 'Emulation.setDeviceMetricsOverride',
    { width: 1680, height: 1400, deviceScaleFactor: 1, mobile: false });
  // 收集未捕获异常
  // ⚠️ 必须用 Page.addScriptToEvaluateOnNewDocument，而不是 Runtime.evaluate：
  //    新建标签页时导航可能尚未完成，直接 evaluate 注进去的 window.__errs
  //    会被随后的文档加载**整个清掉** → 最后读到 undefined（本脚本栽过）。
  //    前者会在**每个新文档**里执行，天然免疫这个时序问题。
  await rpc(ws, 'Page.addScriptToEvaluateOnNewDocument', {
    source: `window.__errs = [];
      window.addEventListener('error', e => window.__errs.push(String(e.message)));`,
  });
  // ⚠️ 上面注册的脚本只对**之后创建**的文档生效。而标签页是用
  //    /json/new?<url> 打开的，文档可能已经在创建了 → 必须显式再导航一次，
  //    否则脚本不会执行，最后读到 undefined（本脚本栽过两次）。
  await rpc(ws, 'Page.navigate', { url: URL_APP });
  await sleep(500);

  let pass = 0, fail = 0;
  const ok = (c, m) => { c ? pass++ : (fail++, console.log('  ✗ ' + m)); };

  // ---- 等「已连接」+ 等 #run 按钮可用（关键）
  const waitFor = async (expr, ms, label) => {
    const t0 = Date.now();
    while (Date.now() - t0 < ms) {
      let v = null;
      try { v = await evalJS(ws, expr); } catch (e) { v = null; }  // 元素未就绪时吞掉
      if (v) return true;
      await sleep(1500);
    }
    console.log(`  ⏱ 超时等待：${label}`);
    return false;
  };

  console.log('【0】等 DOM 就绪');
  // ⚠️ 新建标签页后要先等文档结构出现，否则 #netinfo 为 null 直接抛异常
  const domReady = await waitFor(`!!document.querySelector('#netinfo')`, 60000, 'DOM 就绪');
  ok(domReady, '#netinfo 元素应出现');

  console.log('【1】连接数据服务');
  ok(await waitFor(`/已连接/.test(document.querySelector('#netinfo').textContent)`,
    30000, 'netinfo 已连接'), 'netinfo 应变为「已连接」');

  console.log('【2】等待首次回测完成（#run 可用）');
  const ready0 = await waitFor(`!document.querySelector('#run').disabled &&
    document.querySelector('#p-bt').innerHTML.length > 5000`, 90000, '首次回测完成');
  ok(ready0, '#run 应恢复可用且回测页已渲染');

  const st0 = await evalJS(ws, `document.querySelector('#cap_stat').textContent`);
  ok(/不限仓位/.test(st0), `默认应为不限仓位，实际「${st0}」`);
  const guide = await evalJS(ws,
    `document.querySelector('#p-bt').innerHTML.includes('请在左侧')`);
  ok(guide === true, '不限仓位时回测页应提示去左侧开启约束');

  console.log('【3】切到「限制持仓」10只/日3只 并运行');
  const disp = await evalJS(ws, `(() => {
    const b = [...document.querySelectorAll('#seg-cap button')].find(x => x.dataset.v === '1');
    b.click();
    document.querySelector('#cap_maxpos').value = '10';
    document.querySelector('#cap_maxnew').value = '3';
    document.querySelector('#cap_maxpos').dispatchEvent(new Event('input', {bubbles:true}));
    return document.querySelector('#capwrap').style.display;
  })()`);
  ok(disp === 'block', `参数区应显示，实际 ${disp}`);

  await evalJS(ws, `document.querySelector('#run').click(); 'clicked'`);
  const ready1 = await waitFor(
    `document.querySelector('#p-bt').innerHTML.includes('仓位约束结果')
     || document.querySelector('#p-bt').innerHTML.includes('回测失败')`,
    240000, '容量回测完成');
  const btHtml = await evalJS(ws, `document.querySelector('#p-bt').innerHTML`);
  ok(ready1 && !btHtml.includes('回测失败'),
     `应渲染「仓位约束结果」；长度 ${btHtml.length}，头部：${strip(btHtml).slice(0, 160)}`);

  const txt = strip(btHtml);
  console.log('【4】双口径数值');
  for (const [k, re] of [
    ['已投资金口径', /已投资金口径/],
    ['账户资金口径', /账户资金口径/],
    ['账户口径 CAGR 31.2%', /31\.2\s*%/],
    ['已投资金 CAGR 35.4%', /35\.4\s*%/],
    ['建仓 787 笔', /787/],
    ['丢弃 99.2%', /99\.2\s*%/],
    ['平均持仓 8.6', /8\.6/],
    ['z 值 3.98', /3\.98/],
    ['显著优于随机', /显著优于随机/],
    ['资金利用率', /资金利用率/],
    ['IS/OOS 受限行', /受限 10只\/日3只/],
  ]) ok(re.test(txt), `${k} 未出现`);

  console.log('【5】真实 ECharts 实例已渲染');
  // ⚠️ 必须先切到「策略回测」Tab：
  //    #p-bt 初始不是 .pane.on，图表在 display:none 的容器里初始化会退化成
  //    100px 宽。前端已在切 Tab 时 setTimeout(resize, 40) 修正，
  //    所以这里要先切 Tab、等 resize 完再断言，否则是自己的测试错。
  await evalJS(ws, `(() => {
    const b = [...document.querySelectorAll('#tabs button')].find(x => x.dataset.t === 'bt');
    b.click(); return 'ok';
  })()`);
  await sleep(900);
  const charts = await evalJS(ws, `(() => {
    const out = {};
    ['ch-nav','ch-dd','ch-year','ch-seg'].forEach(id => {
      const el = document.getElementById(id);
      const cv = el && el.querySelector('canvas');
      out[id] = cv ? (cv.width + 'x' + cv.height) : 'no-canvas';
    });
    return out;
  })()`);
  Object.entries(charts).forEach(([k, v]) =>
    ok(/^\d+x\d+$/.test(v) && parseInt(v) > 300, `${k} 画布异常: ${v}`));
  console.log('   画布尺寸:', JSON.stringify(charts));

  console.log('【6】净值图含受限曲线');
  const navSeries = await evalJS(ws, `(() => {
    const c = echarts.getInstanceByDom(document.getElementById('ch-nav'));
    return c ? c.getOption().series.map(s => s.name) : null;
  })()`);
  ok(Array.isArray(navSeries) && navSeries.some(n => n && n.includes('受限仓位')),
     `净值图应有受限曲线，实际 ${JSON.stringify(navSeries)}`);
  console.log('   净值图系列:', JSON.stringify(navSeries));

  console.log('【7】逐年图含受限系列');
  const yrSeries = await evalJS(ws, `(() => {
    const c = echarts.getInstanceByDom(document.getElementById('ch-year'));
    return c ? c.getOption().series.map(s => s.name) : null;
  })()`);
  ok(Array.isArray(yrSeries) && yrSeries.some(n => n && n.includes('受限')),
     `逐年图应有受限系列，实际 ${JSON.stringify(yrSeries)}`);

  console.log('【8】交易明细页用容量口径（不再是不限仓位）');
  await evalJS(ws, `(() => {
    const b = [...document.querySelectorAll('#tabs button')].find(x => x.dataset.t === 'trades');
    b.click(); return 'ok';
  })()`);
  const trReady = await waitFor(
    `(document.querySelector('#tbody-trades') || {}).children !== undefined
     && document.querySelector('#tbody-trades').children.length > 0`,
    180000, '交易明细渲染完成');
  ok(trReady, '交易明细应渲染出行');
  const trTxt = strip(await evalJS(ws, `document.querySelector('#p-trades').textContent`));
  for (const [k, re] of [
    ['口径为仓位约束', /仓位约束/],
    ['不再出现「不限仓位」警示', /实盘做不完这么多/],
    ['持仓上限 10 只', /同时最多持 10 只/],
    ['每日买入 3 只', /每日最多买 3 只/],
    ['诊断卡「本次实际建仓」', /本次实际建仓/],
    ['诊断卡「期末未平仓」', /期末未平仓/],
    ['未平仓说明', /超出了面板范围|还拿在手上/],
  ]) {
    if (k === '不再出现「不限仓位」警示') ok(!re.test(trTxt), '交易明细仍显示「不限仓位」警示');
    else ok(re.test(trTxt), `交易明细：${k} 未出现`);
  }
  // 🔴 屏幕上的「共 N 笔」必须等于回测页的建仓数减未平仓
  //    ⚠ 2026-09-25 起容量口径改为「同一只票未平仓期间不得重复买入」，
  //      建仓数仍是 787，但边缘那笔的到期日被推后 → 未平仓 6 → 7，可结算 781 → 780。
  //      数值变了请先跑 make_fixtures.py 并检查是口径变更还是回归。
  ok(/共\s*780\s*笔/.test(trTxt.replace(/,/g, '')),
     `明细总笔数应为 780（= 787 建仓 − 7 未平仓），实际「${(trTxt.match(/共\s*[\d,]+\s*笔/) || [''])[0]}」`);

  console.log('【9】运行时无未捕获异常');
  const errs = await evalJS(ws, `window.__errs`);
  ok(Array.isArray(errs) && errs.length === 0,
     `不应有未捕获异常，实际 ${JSON.stringify(errs)}`);

  console.log(`\n${fail === 0 ? '✅' : '❌'}  真实 Chrome 容量约束：${pass}/${pass + fail} 通过`);
  try { await rpc(ws, 'Page.close'); } catch (e) {}
  ws.close();
  process.exit(fail === 0 ? 0 : 1);
})().catch(e => { console.error('FATAL', e.message); process.exit(2); });
