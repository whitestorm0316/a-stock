#!/usr/bin/env node
/* 运行时冒烟测试: 桩掉 document / echarts, 跑一遍内联 JS,
   记录每个 setOption 调用, 捕获任何运行时异常。 */
const fs = require('fs');

const code = fs.readFileSync(process.argv[2], 'utf8');

const made = [];      // 已初始化的图表 id
const opts = {};      // id -> option

function mkEl(id) {
  return {
    id: id,
    style: {},
    children: [],
    innerHTML: '',
    className: '',
    value: '',
    selected: false,
    appendChild(c) { this.children.push(c); },
    addEventListener() {},
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
  console.log(e.stack.split('\n').slice(0, 6).join('\n'));
}

console.log('charts init: ' + made.length);
console.log('setOption calls: ' + nopt);
console.log('ids: ' + made.join(', '));

/* 逐图检查 series data 是否有真实数值 */
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

if (bad.length) {
  console.log('\n⚠ 可疑图表: ' + bad.join(' | '));
}
if (failed) process.exit(1);
console.log('\n' + (bad.length ? '⚠ 有可疑图表' : '✅ 全部图表数据非空'));

/* 追加: 检查动态生成的表格是否正确注入 */
console.log('\n--- 动态表格 ---');
['tbl-geo','tbl-main','tbl-period','tbl-events','tbl-score-rho','tbl-score-th'].forEach(function(id){
  var el = els[id];
  var n = el ? String(el.innerHTML||'').length : -1;
  var rows = el ? (String(el.innerHTML).match(/<tr>/g)||[]).length : 0;
  console.log((n>50 ? '  ✓ ' : '  ✗ ') + id + '  chars=' + n + ' rows=' + rows);
});
console.log('--- tab 容器 ---');
['tabs-univ','tabs-strat-period','tabs-score-th'].forEach(function(id){
  var el = els[id];
  var n = el ? (el.children||[]).length : -1;
  console.log((n>0 ? '  ✓ ' : '  ✗ ') + id + '  buttons=' + n);
});
