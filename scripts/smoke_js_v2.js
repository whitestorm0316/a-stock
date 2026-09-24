#!/usr/bin/env node
/* V2 运行时冒烟测试: 桩掉 document / echarts, 跑一遍内联 JS,
   记录每个 setOption 调用, 捕获任何运行时异常, 检查动态表格注入。 */
const fs = require('fs');

const code = fs.readFileSync(process.argv[2], 'utf8');

const made = [];
const opts = {};

function mkEl(id) {
  return {
    id: id,
    style: {},
    children: [],
    innerHTML: '',
    className: '',
    value: '',
    selected: false,
    _attrs: {},
    appendChild(c) { this.children.push(c); },
    addEventListener() {},
    querySelectorAll(sel) { return []; },
    setAttribute(k, v) { this._attrs[k] = v; },
    getAttribute(k) { return this._attrs[k]; },
    set onchange(f) { this._onchange = f; },
    get onchange() { return this._onchange; }
  };
}

const els = {};
global.document = {
  getElementById(id) {
    if (!els[id]) els[id] = mkEl(id);
    return els[id];
  },
  createElement(tag) { return mkEl('new:' + tag); }
};
global.window = { addEventListener() {} };

let nopt = 0;
global.echarts = {
  init(el) {
    const id = el && el.id;
    made.push(id);
    const inst = {
      setOption(o) { nopt++; opts[id] = o; },
      resize() {}
    };
    els['__inst__' + id] = inst;
    return inst;
  },
  getInstanceByDom(el) { return el ? els['__inst__' + el.id] : null; }
};

let failed = false;
try {
  eval(code);
} catch (e) {
  failed = true;
  console.log('❌ RUNTIME ERROR: ' + e.message);
  console.log(e.stack.split('\n').slice(0, 8).join('\n'));
}

console.log('charts init: ' + made.length);
console.log('setOption calls: ' + nopt);

let bad = [];
made.forEach(function (id) {
  const o = opts[id];
  if (!o || !o.series) { bad.push(id + ' (no series)'); return; }
  const arr = Array.isArray(o.series) ? o.series : [o.series];
  let total = 0, okv = 0;
  arr.forEach(function (s) {
    const d = s.data || [];
    total += d.length;
    d.forEach(function (v) {
      const x = (v && typeof v === 'object' && !Array.isArray(v)) ? v.value : (Array.isArray(v) ? v[v.length - 1] : v);
      if (x !== null && x !== undefined && !Number.isNaN(Number(x))) okv++;
    });
  });
  if (total === 0) bad.push(id + ' (empty data)');
  else if (okv === 0) bad.push(id + ' (all-null data)');
  else console.log('  ✓ ' + id + '  pts=' + total + ' valid=' + okv);
});

if (bad.length) console.log('\n⚠ 可疑图表: ' + bad.join(' | '));

/* 检查 V2 动态表格 */
console.log('\n--- 动态表格 (V2) ---');
const TBLS = ['tbl-ev','tbl-voldir','tb-pos','tb-hv','tb-ic','tb-size','tb-ind','tb-reg',
              'tb-en','tb-ex1','tb-abl','tb-wf','tb-oos','tb-bench','tb-funnel',
              'tb-simple','tb-neigh'];
let tbad = [];
TBLS.forEach(function(id){
  const el = els[id];
  const s = el ? String(el.innerHTML||'') : '';
  const rows = (s.match(/<tr>/g)||[]).length;
  if (rows > 0) console.log('  ✓ ' + id + '  chars=' + s.length + ' rows=' + rows);
  else { console.log('  ✗ ' + id + '  chars=' + s.length + ' rows=0'); tbad.push(id); }
});

/* 检查 tab 容器 */
console.log('\n--- tab 容器 ---');
['tabs-vol','tabs-pos'].forEach(function(id){
  const el = els[id];
  const n = el ? String(el.innerHTML||'').length : -1;
  console.log((n > 50 ? '  ✓ ' : '  ✗ ') + id + '  htmlLen=' + n);
  if (n <= 50) tbad.push(id);
});

if (failed) process.exit(1);
console.log('\n' + ((bad.length||tbad.length) ? '⚠ 有问题: 图=' + bad.length + ' 表=' + tbad.length
  : '✅ 全部图表数据非空, 全部动态表格已注入'));
