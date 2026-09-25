/* 图标体系验证（jsdom + 文件层校验）
 *
 * 覆盖点：
 *   1. <head> 里 favicon / apple-touch / manifest / theme-color 齐全且指向真实文件
 *   2. 头部 h1 内联了 .brand 字形，标题文字未被改动
 *   3. CSS 里有 .brand 的尺寸规则
 *   4. SVG 本身合法：viewBox 正确、概念图可互换（几何一致）
 *   5. PNG 真的按请求尺寸渲出来（读 IHDR，防止 Chrome 的 DPI 缩放偷改尺寸）
 *   6. site.webmanifest 合法，图标路径全部落到真实文件
 *
 * 为什么第 5 条重要：headless Chrome 的 --force-device-scale-factor 一旦失效，
 * 512 的图会被渲成 1024 或 384 —— 肉眼在浏览器里看不出来，但 iOS 主屏图标会糊。
 */
const fs = require('fs');
const path = require('path');
const ROOT = path.resolve(__dirname, '..');
const APP = path.join(ROOT, 'app');
const ICONS = path.join(APP, 'icons');
const html = fs.readFileSync(path.join(APP, 'index.html'), 'utf8');
const { JSDOM } = require(path.join(ROOT, 'node_modules/jsdom'));

const errors = [];
const dom = new JSDOM(html, { runScripts: 'dangerously', url: 'http://127.0.0.1:8771/',
  beforeParse(w) {
    w.HTMLElement.prototype.scrollIntoView = () => {};
    w.requestAnimationFrame = cb => setTimeout(() => cb(Date.now()), 0);
    w.echarts = { init: () => ({ setOption() {}, resize() {}, dispose() {}, getDom: () => null }) };
    w.fetch = () => new Promise(() => {});
    w.onerror = (m, s, l, c, e) => errors.push(String(e || m));
    w.addEventListener('error', e => errors.push(String(e.message || e)));
  } });

const $ = s => dom.window.document.querySelector(s);
const $$ = s => [...dom.window.document.querySelectorAll(s)];
const out = [];
const warns = [];
let n = 0;
function ok(label, cond, detail) {
  n++;
  out.push(`${cond ? '✅' : '❌'} ${String(n).padStart(2, '0')}. ${label}` + (detail ? `  — ${detail}` : ''));
  if (!cond) warns.push(label);
}

function headLink(rel) {
  return $$('link').find(l => (l.getAttribute('rel') || '') === rel);
}
const staticToFile = href => path.join(APP, href.replace(/^\/static\//, ''));

setTimeout(() => {
  // ---- 1. head 资源
  const svgLink = $$('link').filter(l => l.getAttribute('rel') === 'icon')
    .find(l => l.getAttribute('type') === 'image/svg+xml');
  ok('favicon 有 SVG 版本', !!svgLink && svgLink.getAttribute('href') === '/static/icons/icon.svg',
     svgLink ? svgLink.getAttribute('href') : '缺失');

  const png32 = $$('link').filter(l => l.getAttribute('rel') === 'icon')
    .find(l => l.getAttribute('sizes') === '32x32');
  ok('favicon 有 32×32 PNG 兜底', !!png32, png32 ? png32.getAttribute('href') : '缺失');

  const apple = headLink('apple-touch-icon');
  ok('apple-touch-icon 在（iOS 加到主屏要 PNG）', !!apple && /180x180/.test(apple.getAttribute('sizes') || ''),
     apple ? apple.getAttribute('sizes') : '缺失');

  const mani = headLink('manifest');
  ok('manifest 已挂', !!mani && mani.getAttribute('href') === '/static/site.webmanifest',
     mani ? mani.getAttribute('href') : '缺失');

  const tc = $('meta[name="theme-color"]');
  ok('theme-color 与图标底色一致（#22406e）', !!tc && tc.getAttribute('content') === '#22406e',
     tc ? tc.getAttribute('content') : '缺失');

  // ---- 2. 所有 /static/ 链接都落到真实文件（防手写路径打错字）
  const hrefs = $$('[href^="/static/"]').map(e => e.getAttribute('href'));
  const missing = hrefs.filter(h => !fs.existsSync(staticToFile(h)));
  ok('/static/ 链接全部指向真实文件', missing.length === 0,
     missing.length ? '缺：' + missing.join(', ') : `${hrefs.length} 个链接全部命中`);

  // ---- 3. 头部字形
  const h1 = $('#top h1');
  const brand = $('#top h1 .brand');
  ok('头部 h1 内联了 .brand 图标', !!brand, brand ? brand.tagName.toLowerCase() : '缺失');
  ok('标题文字未被动到（字形是 SVG，不污染 textContent）',
     h1 && h1.textContent.trim() === 'A股超跌反转 · 交互式选股器',
     h1 ? JSON.stringify(h1.textContent.trim()) : '—');
  ok('h1 内是内联 SVG 而非 <img>（index.html 保持单文件可用）',
     !!brand && brand.tagName.toLowerCase() === 'svg');

  const paths = brand ? [...brand.querySelectorAll('path')] : [];
  const strokes = paths.map(p => (p.getAttribute('stroke') || '').toLowerCase());
  ok('.brand 含绿色跌段（#2ea06a）', strokes.includes('#2ea06a'), strokes.join(',') || '无');
  ok('.brand 含红色涨段（#e04a4a）', strokes.includes('#e04a4a'), strokes.join(',') || '无');
  ok('.brand 含箭头（fill 红 + transform）',
     paths.some(p => (p.getAttribute('fill') || '').toLowerCase() === '#e04a4a' && p.getAttribute('transform')));
  ok('.brand aria-hidden（装饰性，不该被读屏念出来）',
     brand && brand.getAttribute('aria-hidden') === 'true');
  ok('.brand viewBox 与 icon-mark.svg 一致（同一字形，不会两边跑偏）',
     !!brand && brand.getAttribute('viewBox') === '118 90 300 300',
     brand ? brand.getAttribute('viewBox') : '—');

  // ---- 4. CSS 规则
  ok('CSS 有 #top h1 .brand 尺寸规则', /#top h1 \.brand\{[^}]*width:19px/.test(html));
  ok('h1 改成 flex 布局（图标与文字垂直居中）', /#top h1\{[^}]*display:flex/.test(html));

  // ---- 5. SVG 文件
  const svgName = path.join(ICONS, 'icon.svg');
  const svgTxt = fs.readFileSync(svgName, 'utf8');
  ok('icon.svg viewBox 为 0 0 512 512', /viewBox="0 0 512 512"/.test(svgTxt));
  ok('icon.svg 有圆角底与细边（深色标签栏可分离）',
     /rx="116"/.test(svgTxt) && /stroke-opacity="0\.16"/.test(svgTxt));
  ok('icon.svg 颜色取自应用调色板（--blue/--up/--dn）',
     /#3266ad/.test(svgTxt) && /#e04a4a/.test(svgTxt) && /#2ea06a/.test(svgTxt));

  // 三案几何一致 → 换方案只需改一行 cp，视觉体量不会跳
  const concepts = ['concept-a.svg', 'concept-b.svg', 'concept-c.svg'];
  const geom = concepts.map(f => {
    const t = fs.readFileSync(path.join(ICONS, f), 'utf8');
    return (t.match(/viewBox="[^"]+"/) || [''])[0] + '|' + ((t.match(/rx="116"/) || [''])[0]);
  });
  ok('三个概念图几何一致（可互换）', geom.every(g => g === geom[0]), geom[0] || '—');
  ok('icon.svg 可溯源到某个概念图（未被手改到无出处）',
     concepts.some(f => fs.readFileSync(path.join(ICONS, f), 'utf8') === svgTxt));

  const markTxt = fs.readFileSync(path.join(ICONS, 'icon-mark.svg'), 'utf8');
  ok('icon-mark.svg 裁到字形边缘（viewBox 非整幅）',
     /viewBox="118 90 300 300"/.test(markTxt) && !/rx="116"/.test(markTxt));
  ok('icon-mark.svg 与内联 .brand 的字形路径完全一致', (() => {
    const grab = t => (t.match(/d="M154 152[^"]*"/g) || []).concat(t.match(/transform="translate\(360,180\)[^"]*"/g));
    return JSON.stringify(grab(markTxt)) === JSON.stringify(grab(
      `<svg>${brand ? brand.innerHTML : ''}</svg>`));
  })());

  // ---- 6. PNG 尺寸（读 IHDR）
  const pngSize = f => {
    const b = fs.readFileSync(f);
    if (b.length < 24 || b.readUInt32BE(0) !== 0x89504e47) return null;
    return [b.readUInt32BE(16), b.readUInt32BE(20)];
  };
  const want = [16, 32, 64, 180, 192, 512];
  const bad = [];
  want.forEach(s => {
    const f = path.join(ICONS, `icon-${s}.png`);
    if (!fs.existsSync(f)) { bad.push(`icon-${s}.png 不存在`); return; }
    const sz = pngSize(f);
    if (!sz) bad.push(`icon-${s}.png 不是合法 PNG`);
    else if (sz[0] !== s || sz[1] !== s) bad.push(`icon-${s}.png 实为 ${sz[0]}×${sz[1]}`);
  });
  ok('PNG 全部存在且实际尺寸与文件名一致', bad.length === 0,
     bad.length ? bad.join('; ') : want.map(s => `${s}×${s}`).join(' '));

  // ---- 7. manifest
  let mf = null;
  try { mf = JSON.parse(fs.readFileSync(path.join(APP, 'site.webmanifest'), 'utf8')); }
  catch (e) { mf = null; }
  ok('site.webmanifest 是合法 JSON', !!mf);
  if (mf) {
    const srcs = (mf.icons || []).map(i => i.src);
    const miss = srcs.filter(s => !fs.existsSync(path.join(ICONS, path.basename(s))));
    ok('manifest 里的图标路径全部命中真实文件', miss.length === 0,
       miss.length ? '缺：' + miss.join(', ') : srcs.join(' '));
    ok('manifest 含 192 与 512', srcs.some(s => /192/.test(s)) && srcs.some(s => /512/.test(s)));
    ok('manifest 声明了 maskable（Android 自适应图标）',
       (mf.icons || []).some(i => (i.purpose || '').includes('maskable')));
    ok('manifest start_url / display 合理',
       mf.start_url === '/' && mf.display === 'standalone');
  }

  ok('页面无运行时错误', errors.length === 0, errors.slice(0, 3).join(' | ') || '干净');

  console.log('\n══════ 图标体系验证 ══════\n');
  out.forEach(l => console.log(l));
  const pass = out.filter(l => l.startsWith('✅')).length;
  console.log(`\n通过 ${pass}/${out.length}`);
  if (warns.length) console.log('未通过:', warns.join(' | '));
  process.exit(0);
}, 2600);
