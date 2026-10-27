/* 一次性验证：从运行中的服务拉取首页，确认 /static/echarts.min.js 外链脚本
   真的执行并定义了 window.echarts。用后即弃，不进入正式测试套件。 */
const path = require('path');
const ROOT = path.resolve(__dirname, '..');
const { JSDOM } = require(path.join(ROOT, 'node_modules/jsdom'));

const URL = process.env.CHECK_URL || 'http://127.0.0.1:8770/';

JSDOM.fromURL(URL, {
  runScripts: 'dangerously',
  resources: 'usable',
  pretendToBeVisual: true,
}).then(dom => {
  const w = dom.window;
  const done = (code) => { try { dom.window.close(); } catch (e) {} process.exit(code); };

  // 外链脚本是异步抓取的：轮询到 echarts 出现或超时
  const t0 = Date.now();
  const tick = setInterval(() => {
    const ok = typeof w.echarts === 'object' && w.echarts && typeof w.echarts.init === 'function';
    if (ok) {
      clearInterval(tick);
      console.log(`[OK] window.echarts 已定义，version=${w.echarts.version} 耗时 ${Date.now() - t0}ms`);
      const tags = [...w.document.querySelectorAll('script[src]')].map(s => s.getAttribute('src'));
      console.log('[OK] 外链 script:', JSON.stringify(tags));
      done(0);
    } else if (Date.now() - t0 > 20000) {
      clearInterval(tick);
      console.log('[FAIL] 20s 内 window.echarts 仍未定义');
      console.log('[FAIL] 外链 script:', JSON.stringify(
        [...w.document.querySelectorAll('script[src]')].map(s => s.getAttribute('src'))));
      done(1);
    }
  }, 100);
}).catch(e => {
  console.log('[FAIL] 页面加载异常:', e && e.message);
  process.exit(1);
});
