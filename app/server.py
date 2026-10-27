#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server.py —— 「A股超跌反转」交互式选股器 本地服务

启动：python app/server.py            （默认 127.0.0.1:8770）
      python app/server.py 9000       （自定义端口）

接口：
  GET  /                    前端界面
  GET  /api/meta            元信息（交易日范围/行业/股票清单）
  GET  /api/market          市场环境序列（自建净值 vs 均线，口径/窗口可选）+ 真实指数对照
                            查询参数：days / index（all|small50|…）/ ma（20|60|120）
  GET  /api/defaults        默认参数 + 内置预设 + 「我的方案」
  POST /api/scan            今日选股
  POST /api/backtest        策略回测
  POST /api/tune            参数寻优（简单网格）
  GET  /api/stock?code=xxx  单股透视
  POST /api/export          导出候选股 CSV
  POST /api/presets/save    保存当前参数为自定义方案（同名覆盖）
  POST /api/presets/delete  删除自定义方案
  GET  /api/holdings        实盘持仓账本 + 按最新收盘价现算的浮动盈亏
  POST /api/positions       当前持仓（回测口径）：当前参数下「持有期未走完」的头寸
                            + 最新价 / 浮动收益率 / 还需交易日 / **预计卖出日**
  POST /api/holdings/save   新增/修改一笔持仓批次（代码/成本/股数/买入日）
  POST /api/holdings/delete 删除一笔持仓批次

「我的方案」落在 data/user_presets.json（跟着数据目录走，不丢在浏览器缓存里）。
「实盘持仓」落在 data/holdings.json；**盈亏不落盘**，每次请求按最新行情现算。
"""
import os
import sys
import io
import csv
import json
import time
import gzip
import threading
import subprocess
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 必须在第一次 print 之前：中文 Windows 的 cmd.exe 默认 GBK 代码页，
# print 中文会乱码、print emoji（✅ 🚀）会直接 UnicodeEncodeError 崩掉。
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from _console import bootstrap, mark  # noqa: E402
bootstrap()

from engine import (Engine, DEFAULT_PARAMS, PICK_RULES, LADDER_PRESETS,  # noqa: E402
                    RHYTHM_CANDS, CAP_KEYS, rhythm_label,
                    parse_ladder, SIZE_N, SIZE_PCT, norm_size_band, size_band_label)

import numpy as np  # noqa: E402

APP = os.path.dirname(os.path.abspath(__file__))
_html_cache = {}

# /static/ 的扩展名 → Content-Type 映射。
# 容器（BaseHTTPRequestHandler）默认靠 extensions_map 猜类型，但对 .svg /
# .webmanifest 会退回 application/octet-stream —— 浏览器拿到 octet-stream 的
# favicon 会直接拒绝渲染，所以这里显式指定。
_STATIC_CTYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".svg": "image/svg+xml; charset=utf-8",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}

print("=" * 72)
print("  正在加载面板并预计算（约 60~90 秒，请稍候）...")
print("=" * 72, flush=True)
ENGINE = Engine()
print(f"  {mark('✅', '[OK]')} 就绪：{ENGINE.C['n']:,} 行 / {len(ENGINE.C['starts']):,} 只 / "
      f"{ENGINE.nd:,} 交易日 / 耗时 {ENGINE.build_ms/1000:.1f}s", flush=True)

# 净值曲线按周采样（前端足够平滑，体积降 80%）
STEP = max(1, ENGINE.nd // 1400)
DAY_STR = [str(x) for x in ENGINE.day_str[::STEP]]


def _curve(nv):
    return [round(float(x), 4) for x in np.asarray(nv)[::STEP]]


def _curve_net(net):
    """日收益数组 → 采样后的净值曲线"""
    return _curve(np.cumprod(1.0 + np.asarray(net, float)))


def _clean(obj):
    """把 numpy 类型与 NaN 转成 JSON 安全值"""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating, np.bool_)):
        obj = obj.item()
    if isinstance(obj, float):
        return None if not np.isfinite(obj) else obj
    if isinstance(obj, np.ndarray):
        return _clean(obj.tolist())
    return obj


def _signal_sig(p):
    """信号参数指纹 —— **容量参数（CAP_KEYS）不参与**。

    为什么能这么切：容量参数（max_pos / max_new / pick / cap_ladder）只决定
    「信号怎么变成持仓」，**完全不改变 mask**。所以寻优时同一批信号只需要算
    一次 `stats()`，剩下的节奏变体各自跑一次轻量 `capacity()` 即可 ——
    实测把「节奏 × 3 个信号轴」从 240 次 stats（约 3.5 分钟）压到 27 次（约 20 秒）。

    ⚠️ 反过来，`mkt_index` / `mkt_ma` **必须**参与指纹 —— 它们改变市场过滤条件，
       直接改变 mask。这里靠「只排除 CAP_KEYS」自动带上，不需要额外处理；
       但若哪天有人把它们加进 CAP_KEYS，缓存会**静默串味**（换口径后仍返回旧结果）。

    ⚠️ `sort_keys=True` 必须开：dict 顺序会随构造路径变化，不排序会得到
       两个内容相同却指纹不同的键，缓存直接失效（命中率 0，白算一遍）。
    """
    d = {k: v for k, v in p.items() if k not in CAP_KEYS}
    return json.dumps(d, sort_keys=True, default=str)


# ---------------------------------------------------------------- 真实指数（对照用）
# 用途：界面的「牛熊」结论来自**自建全A等权净值**（`engine.market_oret`，日度再平衡、
# 无成本），它与上证/沪深300 这类**市值加权价格指数**在个别时段会明显分歧。
# 分歧大时用户会直接质疑「今天明明是熊市，界面为什么说不是」——
# 所以把真实指数的同口径（收盘 vs 自身 MA60）一并回传，让用户能自己对照。
#
# ⚠️ 这些文件只覆盖 2023 起（数据源窗口限制，见 scripts/01c_fetch_index.py），
#    因此**不能**用作 11 年回测的市场过滤依据，只做展示/对照。
# ⚠️ 文件缺失时静默跳过（它们是可选数据），不要让整个接口 500。
_INDEX_FILES = [
    ("上证指数", "index_sh000001.json"),
    ("沪深300", "index_hs300.json"),
    ("中证500", "index_zz500.json"),
    ("中证1000", "index_zz1000.json"),
    # 同花顺全A：**市值加权**口径，是这张对照表里唯一的「全市场 + 加权」指数。
    # ⚠️ 历史只到 2023-12 起（数据源窗口），因此和其他四个一样**只做展示**，
    #    不参与任何回测计算。它的价值在于让用户看到「等权 vs 市值加权」差多少：
    #    同期（2023-12~2026-10）市值加权 +11.7% vs 自建等权 +15.7%，差约 4pp。
    ("同花顺全A", "index_ths_allA.json"),
]
_index_cache = {}


def _indices_sig():
    """所有指数文件的 (mtime, size) 指纹 —— 缓存有效性判据。

    ⚠️ 为什么要指纹而不是「永久缓存」
    --------------------------------
    原先 `_index_cache` 一旦填了就**永不过期** → 即使指数文件被更新
    （`01c_fetch_index.py` 重跑、或每日更新补上这一步），界面**仍然显示旧数据**，
    非重启不可。而用户看到的症状是「自建净值更新了、真实指数没更新」——
    会以为是取数失败，实则是**进程内的缓存没失效**。

    指纹选 mtime+size 而不是内容哈希：指数文件 ~150KB，每次请求都算哈希太浪费；
    而 mtime+size 对「文件被重写」这个场景足够可靠（重写必然改 mtime）。
    """
    parts = []
    for _n, fn in _INDEX_FILES:
        try:
            st = os.stat(os.path.join(ROOT, "data", "raw", fn))
            parts.append(f"{fn}:{int(st.st_mtime)}:{st.st_size}")
        except OSError:
            parts.append(f"{fn}:-")
    return "|".join(parts) + f"#days={int(days_sig_holder[0])}"


# 小技巧：把 days 放进签名（`_indices_sig` 需要它，但又不想多加参数）
days_sig_holder = [310]


def _load_indices(days=310):
    """真实指数 → [{name, last_date, close, ma60, dist, bull, series:[{date,close,ma60,dist}]}]。

    结果按 `days` 缓存，但**带文件指纹校验**：指数文件一变就重算（见 `_indices_sig`）。
    这样「更新数据 → 指数刷新」不需要重启服务。
    """
    key = int(days)
    days_sig_holder[0] = key
    sig = _indices_sig()
    hit = _index_cache.get(key)
    if hit and hit[0] == sig:
        return hit[1]
    out = []
    for name, fn in _INDEX_FILES:
        fp = os.path.join(ROOT, "data", "raw", fn)
        try:
            with open(fp, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(raw, list) or not raw:
            continue
        rows = []
        for it in raw:
            try:
                ts = int(it["date_ms"]) / 1000.0
                rows.append((time.strftime("%Y-%m-%d", time.gmtime(ts + 8 * 3600)),
                             float(it["close_price"])))
            except (KeyError, TypeError, ValueError):
                continue
        if len(rows) < 61:
            continue
        rows.sort(key=lambda x: x[0])
        # 与引擎同口径：收盘价 vs 自身 60 日均线（简单均线，非指数均线）
        closes = np.array([c for _, c in rows], float)
        ma = np.full(len(closes), np.nan)
        cs = np.cumsum(np.concatenate([[0.0], closes]))
        for i in range(59, len(closes)):
            ma[i] = (cs[i + 1] - cs[i + 1 - 60]) / 60.0
        series = []
        for i in range(max(0, len(rows) - key), len(rows)):
            d, c = rows[i]
            series.append(dict(
                date=d, close=round(c, 2),
                ma60=(round(float(ma[i]), 2) if np.isfinite(ma[i]) else None),
                dist=(round((c / ma[i] - 1) * 100, 2) if np.isfinite(ma[i]) else None),
            ))
        last = series[-1]
        out.append(dict(
            name=name, last_date=last["date"], close=last["close"],
            ma60=last["ma60"], dist=last["dist"],
            # ma60 未成形时 bull 给 None（**不是 False**）——「没有均线」≠「跌破均线」
            bull=(None if last["ma60"] is None else bool(last["close"] > last["ma60"])),
            series=series,
        ))
    # 缓存「(指纹, 结果)」二元组 —— 指纹对不上就重算（见 _indices_sig）
    _index_cache[key] = (sig, out)
    return out


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if "/api/" in (self.path or ""):
            sys.stderr.write(f"  {self.command} {self.path}  {args[1] if len(args)>1 else ''}\n")

    # ------------------------------------------------------------ 工具
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        # gzip：净值曲线很长，压缩后体积降 80%
        if len(body) > 2048 and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            body = gzip.compress(body, 6)
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Encoding", "gzip")
        else:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(_clean(obj), ensure_ascii=False,
                                    default=str), "application/json; charset=utf-8")

    def _body(self):
        ln = int(self.headers.get("Content-Length") or 0)
        if ln <= 0:
            return {}
        raw = self.rfile.read(ln)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    # ------------------------------------------------------------ 路由
    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        try:
            if p in ("/", "/index.html"):
                return self._serve_file("index.html", "text/html; charset=utf-8")
            if p == "/api/meta":
                return self._json(ENGINE.meta())
            if p == "/api/market":
                q = parse_qs(u.query)
                try:
                    days = int((q.get("days") or ["250"])[0])
                except (TypeError, ValueError):
                    days = 250
                days = max(30, min(days, 2000))
                # 口径 / 窗口：非法值由 engine.mkt_cfg() 规范化回默认，不会 500
                d = ENGINE.market_series(days, (q.get("index") or [None])[0],
                                         (q.get("ma") or [None])[0])
                # 真实指数一并回传（对照用；文件缺失时是空列表，不是错误）
                d["indices"] = _load_indices(min(days + 60, 2000))
                return self._json(d)
            if p == "/api/stock":
                q = parse_qs(u.query)
                code = (q.get("code") or [""])[0]
                # 仅 code → 用 DEFAULT_PARAMS 且不限池（向后兼容旧链接/书签）
                d = ENGINE.stock_detail(code)
                return self._json(d or {"error": "未找到该股票"}, 200 if d else 404)
            if p == "/api/update/status":
                st = _read_update_state() or {"state": "idle", "msg": ""}
                st["server_last_date"] = ENGINE.meta().get("last_date")
                return self._json(st)
            if p == "/api/alert/status":
                # 每日播报调度器的状态（诊断用：确认它有没有在跑、上次何时触发）
                en, hh, mm = _alert_sched_cfg()
                st = _read_alert_state()
                now = SCHED.now_cst()
                st.update(
                    enabled=en, at=f"{hh:02d}:{mm:02d}",
                    running=bool(_alert_thread and _alert_thread.is_alive()),
                    now=now.strftime("%Y-%m-%d %H:%M:%S"),
                    next_due=SCHED.next_due(now, en, hh, mm))
                return self._json(st)
            if p == "/api/defaults":
                return self._json(dict(
                    defaults=DEFAULT_PARAMS,
                    presets=PRESETS,
                    # 「我的方案」：用户在界面上保存的自定义参数
                    user_presets=load_user_presets(),
                    # 界面默认选中的方案 id（'' = 未设置，前端回退到内置第一套）。
                    # ⚠️ 可能指向一个**已不存在**的 id（文件被手改 / 方案被删）——
                    #    前端必须做存在性校验后再用，不能直接 sel.value = 它。
                    default_preset=load_default_preset(),
                    # 市值区间的合法范围（前端双滑块用，避免两端各写一份百分比表）
                    size_bands=dict(n=SIZE_N, pct=SIZE_PCT),
                    # 容量约束：选股规则清单（供前端下拉）
                    pick_rules=[dict(id=k, name=v["name"], desc=v["desc"],
                                     default=(k == DEFAULT_PARAMS.get("pick")))
                                for k, v in PICK_RULES.items()
                                if not k.startswith("__")],
                    # 建仓节奏：阶梯建仓「每批只数」预设（供前端下拉）
                    ladder_presets=[dict(id=x["id"], name=x["name"],
                                         ladder=x["ladder"], tag=x.get("tag", ""),
                                         desc=x["desc"]) for x in LADDER_PRESETS],
                    # 建仓节奏：「参数寻优」页节奏轴的候选清单（供前端渲染说明文字）
                    # ⚠️ 与 /api/tune 的 AX["rhythm"] 同源（都是 RHYTHM_CANDS），
                    #    前端只负责显示，不自己维护一份候选 → 不会出现「说明写了 8 档、
                    #    实际跑了 10 档」这种漂移。
                    rhythm_cands=[dict(id=x["id"], name=rhythm_label(x),
                                       max_pos=int(x.get("max_pos") or 0),
                                       max_new=int(x.get("max_new") or 0),
                                       ladder=(list(x["ladder"]) if x.get("ladder") else None))
                                  for x in RHYTHM_CANDS],
                    optbest=ENGINE.meta()["last_date"]))
            if p == "/api/holdings":
                return self._holdings_get()
            if p.startswith("/static/"):
                return self._serve_file(p[len("/static/"):], None)
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            import traceback
            traceback.print_exc()
            return self._json({"error": str(e)}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        try:
            b = self._body()
            if p == "/api/scan":
                return self._scan(b)
            if p == "/api/stock":
                return self._stock(b)
            if p == "/api/backtest":
                return self._backtest(b)
            if p == "/api/trades":
                return self._trades(b)
            if p == "/api/tune":
                return self._tune(b)
            if p == "/api/export":
                return self._export(b)
            if p == "/api/presets/save":
                return self._preset_save(b)
            if p == "/api/presets/delete":
                return self._preset_delete(b)
            if p == "/api/presets/set_default":
                return self._preset_set_default(b)
            if p == "/api/holdings/save":
                return self._holding_save(b)
            if p == "/api/positions":
                return self._positions(b)
            if p == "/api/holdings/delete":
                return self._holding_delete(b)
            if p == "/api/update/start":
                port = int(b.get("port") or self.server.server_address[1])
                ok, msg = start_update(port)
                return self._json({"ok": ok, "msg": msg})
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            import traceback
            traceback.print_exc()
            return self._json({"error": str(e)}, 500)

    # ------------------------------------------------------------ 实现
    def _pool(self, b):
        pool, err = ENGINE.build_pool(b.get("pool") or {})
        if err:
            return None, err
        return pool, None

    def _scan(self, b):
        t0 = time.time()
        pool, err = self._pool(b)
        if err:
            return self._json({"error": err}, 200)
        p = dict(DEFAULT_PARAMS)
        p.update(b.get("params") or {})
        res = ENGINE.scan(pool_mask=pool, limit=int(b.get("limit") or 200), p=p)
        res["ms"] = int((time.time() - t0) * 1000)
        return self._json(res)

    def _stock(self, b):
        """单股透视（POST 形态：可携带左侧参数与股票池）。

        请求体：
          code         : "001256.SZ" / "001256"
          params       : 策略参数（同 /api/scan）。**省略则用 DEFAULT_PARAMS**
          pool         : 股票池（同 /api/scan）
          full_history : bool（默认 False）是否附带全历史日线 + 全历史信号日期，
                         供「全部历史」图表用。省略则只返回近 WINDOW 日行情。

        ⚠️ 为什么要有 POST 形态：GET 只能带 code，必然写死默认参数 ——
           于是「左侧把市值放宽到 40%、单股透视却按 30% 算」，两页自相矛盾。
           前端现在把 collect() + currentPool() 一并 POST 过来。

        ⚠️ `signal_dates` **始终是全历史**（用户明确要求看全部历史），
           不在图表窗口内的用 `in_window=false` 标出，前端标灰而非隐藏。
        """
        t0 = time.time()
        code = str(b.get("code") or "").strip()
        if not code:
            return self._json({"error": "缺少 code"}, 200)
        pool, err = self._pool(b)
        if err:
            return self._json({"error": err}, 200)
        p = dict(DEFAULT_PARAMS)
        p.update(b.get("params") or {})
        d = ENGINE.stock_detail(code, p=p, pool_mask=pool,
                                full_history=bool(b.get("full_history")))
        if not d:
            return self._json({"error": "未找到该股票"}, 200)
        d["pool"] = b.get("pool") or {}
        d["ms"] = int((time.time() - t0) * 1000)
        return self._json(d)

    def _backtest(self, b):
        """策略回测。

        请求体：
          params : 策略参数（同 DEFAULT_PARAMS，**含容量约束**）
                     max_pos : 同时持仓上限（0 = 不限，研究报告原始口径）
                     max_new : 每日最多新建仓数
                     pick    : 信号超额时的选股规则（见 PICK_RULES）
          pool   : 股票池
          n_sim  : 随机基准模拟次数（默认 200，仅 max_pos>0 时生效）

        返回：不限仓位口径的 stats()（cagr/mdd/sharpe/...）
              + 容量约束口径的 capacity 块（max_pos>0 时）
        """
        t0 = time.time()
        pool, err = self._pool(b)
        if err:
            return self._json({"error": err}, 200)
        p = dict(DEFAULT_PARAMS)
        p.update(b.get("params") or {})
        hold = int(p.get("hold") or 20)
        mask, conds = ENGINE.build_mask(p, pool)
        st = ENGINE.stats(mask, hold)
        if st is None:
            return self._json({"error": f"信号数过少（{int(mask.sum())}），无法统计。"
                                       f"请放宽条件或检查股票池。"}, 200)
        # 基准：全市场（同股票池）
        base = ENGINE.stats(pool if pool is not None else np.ones(ENGINE.C["n"], bool), hold)
        out = dict(
            ms=int((time.time() - t0) * 1000), conditions=conds,
            n_signal=st["n_signal"], n_trade=st["n_trade"],
            cagr=st["cagr"], mdd=st["mdd"], sharpe=st["sharpe"],
            ann_arith=st["ann_arith"], vol=st["vol"],
            win=st["win"], payoff=st["payoff"], pf=st["pf"],
            mean=st["mean"], median=st["median"], p10=st["p10"], p90=st["p90"],
            avg_holdings=st["avg_holdings"], active_pct=st["active_pct"],
            exw={str(k): v for k, v in st["exw"].items()},
            exw_t={str(k): v for k, v in st["exw_t"].items()},
            yearly=st["yearly"], segs=st["segs"],
            nav=_curve(st["nav"]), nav_gross=_curve(st["nav_gross"]),
            dates=DAY_STR,
            bench=(dict(cagr=base["cagr"], mdd=base["mdd"], sharpe=base["sharpe"],
                        nav=_curve(base["nav"])) if base else None),
            hold=hold,
        )
        # ---- 容量约束回测（实盘可执行性）：max_pos>0 或启用阶梯建仓时
        #      ⚠️ 阶梯建仓（cap_ladder）的满仓只数 = sum(序列)，本身就是一种仓位上限，
        #         单独设它也要生效（此时 max_pos / max_new 都会被推导值覆盖）
        mp = int(p.get("max_pos") or 0)
        lad = parse_ladder(p.get("cap_ladder"))
        if mp > 0 or lad:
            try:
                cap = ENGINE.capacity(
                    mask, hold=hold, max_pos=mp,
                    max_new=int(p.get("max_new") or 3),
                    pick=str(p.get("pick") or "deep"),
                    n_sim=int(b.get("n_sim", 200)),
                    ladder=lad,
                )
                keep = {k: v for k, v in cap.items()
                        if k not in ("net", "net_cap", "cnt", "nav")}
                keep["nav_cap"] = _curve(cap["nav"])       # 账户资金口径净值
                keep["nav_inv"] = _curve_net(cap["net"])   # 已投资金口径净值
                out["capacity"] = keep
            except Exception:
                import traceback
                traceback.print_exc()
                out["capacity"] = None
        else:
            out["capacity"] = None
        # 交易摘要（供回测页直接展示，无需再拉一次明细）
        # ⚠️ 口径必须与上面一致：max_pos>0 时摘要也只统计**实际建仓**的那批，
        #    否则「容量诊断说建仓 787 笔」而「交易摘要说 93,553 笔」，自相矛盾。
        try:
            tplan = None
            if mp > 0 or lad:
                tplan = ENGINE.capacity_plan(
                    mask, hold=hold, max_pos=mp,
                    max_new=int(p.get("max_new") or 3),
                    pick=str(p.get("pick") or "deep"),
                    ladder=lad)
            tr = ENGINE.trades(mask, hold, include_fin=False, plan=tplan)
            if tr:
                s = tr["summary"]
                out["trade_brief"] = dict(
                    n_trade=s["n_trade"], n_stock=s["n_stock"],
                    win=s["win"], mean=s["mean"], median=s["median"],
                    best=s["best"], worst=s["worst"],
                    pf=s["pf"], payoff=s["payoff"],
                    avg_excess=s["avg_excess"], excess_win=s["excess_win"],
                    date_start=s["date_start"], date_end=s["date_end"],
                    hist=tr["hist"], plan=tr.get("plan"),
                    # 未结束交易（不计入上面的任何统计）—— 回测页也照实说明
                    n_open=tr.get("n_open", 0),
                    n_open_holding=tr.get("n_open_holding", 0),
                    n_open_nobuy=tr.get("n_open_nobuy", 0),
                    n_open_stuck=tr.get("n_open_stuck", 0),
                )
        except Exception:
            out["trade_brief"] = None
        return self._json(out)

    def _trades(self, b):
        """逐笔交易明细：分页 / 排序 / 盈亏筛选 / 年份筛选 / 个股筛选。

        请求体：
          params      : 策略参数（同 /api/backtest）
          pool        : 股票池
          sort        : 排序字段（默认 exit_date）
          order       : "asc" | "desc"（默认 desc）
          page        : 页码（1 起）
          page_size   : 每页条数（默认 50，上限 500）
          win         : null | true | false    只看盈利/亏损
          year        : null | int             只看某年
          code        : null | "600519.SH"     只看某只股票
          min_ret/max_ret : 净收益率区间（小数，如 -0.1 = −10%）
          with_rows   : bool  是否返回 rows（默认 true；只想看汇总可设 false）

        两种口径（由 params.max_pos 决定，与 /api/backtest 完全一致）
        ------------------------------------------------------------
        · max_pos=0（默认）→ **不限仓位**：列出全部信号对应的交易
              （默认 K3 为 93,553 笔）；`plan` 返回 null。
        · max_pos>0        → **容量约束**：只列「同时最多持 N 只、每日最多买 M 只」
              时**真正建仓**的那批（默认 K3/10只/日3只 为 787 笔建仓 / 781 笔可结算），
              `plan` 返回该计划的诊断信息（含 `n_open` 未平仓数）。
        """
        t0 = time.time()
        pool, err = self._pool(b)
        if err:
            return self._json({"error": err}, 200)
        p = dict(DEFAULT_PARAMS)
        p.update(b.get("params") or {})
        hold = int(p.get("hold") or 20)
        cost = float(b.get("cost", 0.003))
        mask, conds = ENGINE.build_mask(p, pool)
        if mask.sum() < 50:
            return self._json({"error": f"信号数过少（{int(mask.sum())}），无法列出交易。"
                                       f"请放宽条件。"}, 200)
        # ---- 容量约束（与 /api/backtest 同口径）：max_pos>0 或阶梯建仓时只列实际建仓
        #      ⚠️ 这里用轻量版 capacity_plan()，不跑 200 次随机模拟（明细不需要分布）
        mp = int(p.get("max_pos") or 0)
        lad = parse_ladder(p.get("cap_ladder"))
        plan = None
        if mp > 0 or lad:
            try:
                plan = ENGINE.capacity_plan(
                    mask, hold=hold, max_pos=mp,
                    max_new=int(p.get("max_new") or 3),
                    pick=str(p.get("pick") or "deep"),
                    ladder=lad)
            except Exception:
                import traceback
                traceback.print_exc()
                plan = None
                mp = 0
        tr = ENGINE.trades(mask, hold, cost=cost, plan=plan)
        if tr is None:
            msg = ("该参数下没有任何完成的交易。" if plan is None else
                   f"该仓位约束下没有任何可结算的交易（计划建仓 "
                   f"{len(plan['holds'])} 笔，全部未平仓或被过滤）。请放宽条件。")
            return self._json({"error": msg}, 200)

        rows = tr["rows"]
        # ---- 未结束交易（**独立于筛选**）
        #   ⚠️ 刻意**不**参与下面的 win/year/code/min_ret 过滤：
        #      它们的 net/win 全是 null，一旦跟随筛选就会凭空消失，
        #      而「我明明看到有 57 笔没结算，怎么一筛选就没了」正是要避免的。
        #      页面把它作为独立区块展示，并明说不计入统计。
        open_rows = tr.get("open_rows") or []
        open_sorted = sorted(open_rows,
                             key=lambda r: (str(r.get("signal_date") or ""),
                                            str(r.get("code") or "")),
                             reverse=True)
        # ---- 过滤
        win_f = b.get("win")
        if win_f is not None:
            want = bool(win_f)
            rows = [r for r in rows if r["win"] == want]
        yr = b.get("year")
        if yr:
            y = int(yr)
            rows = [r for r in rows
                    if r["entry_date"] and r["entry_date"][:4] == str(y)]
        cd = b.get("code")
        if cd:
            c = str(cd).strip().upper()
            rows = [r for r in rows if r["code"] == c]
        mn, mx = b.get("min_ret"), b.get("max_ret")
        if mn is not None:
            rows = [r for r in rows if r["net"] >= float(mn) * 100]
        if mx is not None:
            rows = [r for r in rows if r["net"] <= float(mx) * 100]

        n_filt = len(rows)
        # ---- 排序
        sk = b.get("sort") or "exit_date"
        valid_keys = {"exit_date", "entry_date", "signal_date", "code", "name",
                      "net", "ret", "excess", "bench", "buy", "sell", "hold",
                      "fin_rev", "fin_rev_yoy", "fin_np", "fin_np_yoy",
                      "w"}   # w = 该笔占账户资金比例（阶梯建仓）
        if sk not in valid_keys:
            sk = "exit_date"
        rev = (b.get("order") or "desc").lower() != "asc"

        def keyf(r):
            v = r.get(sk)
            if v is None:
                # None 恒排末尾
                return (1, 0.0, "")
            if isinstance(v, bool):
                return (0, 1.0 if v else 0.0, "")
            if isinstance(v, (int, float)):
                return (0, float(v), "")
            return (0, 0.0, str(v))
        rows = sorted(rows, key=lambda r: (keyf(r)[0], keyf(r)[1], keyf(r)[2]),
                      reverse=rev)

        # ---- 分页
        ps = max(1, min(int(b.get("page_size") or 50), 500))
        pg = max(1, int(b.get("page") or 1))
        tot_pg = max(1, (n_filt + ps - 1) // ps)
        pg = min(pg, tot_pg)
        a0 = (pg - 1) * ps
        page_rows = rows[a0:a0 + ps]

        # ---- 过滤后的汇总（与全局汇总分开）
        if n_filt:
            import numpy as _np
            nr = _np.array([r["net"] for r in rows], float) / 100.0
            w = nr > 0
            tl = float(-nr[~w].sum()) if (~w).any() else 0.0
            fsum = dict(
                n=n_filt,
                win=float(w.mean()),
                mean=float(nr.mean()),
                median=float(_np.median(nr)),
                sum=float(nr.sum()),
                pf=(float(nr[w].sum() / tl) if tl > 0 else None),
                best=float(nr.max()), worst=float(nr.min()),
            )
        else:
            fsum = dict(n=0)

        out = dict(
            ms=int((time.time() - t0) * 1000),
            conditions=conds, hold=hold, cost=cost,
            n_trade=tr["n_trade"], n_filtered=n_filt,
            page=pg, page_size=ps, n_page=tot_pg,
            sort=sk, order=("desc" if rev else "asc"),
            summary=tr["summary"], filtered_summary=fsum,
            yearly=tr["yearly"], hist=tr["hist"],
            best=tr["best"], worst=tr["worst"],
            rows=(page_rows if b.get("with_rows", True) else []),
            plan=tr.get("plan"),          # 容量约束信息（不限仓位时为 null）
            # ---- 未结束交易（不计入任何统计，见 engine._open_positions）
            #   ⚠️ n_open = 无结局总数 = holding（真·持有中）+ nobuy（T+1 买不进）
            #      + stuck（买进了但卖出日无价，算不出收益）。三者都要透传，
            #      漏一个会让前端「细分之和 ≠ 总数」，看起来像数据错乱。
            open_rows=(open_sorted if b.get("with_rows", True) else []),
            n_open=len(open_rows),
            n_open_holding=tr.get("n_open_holding", 0),
            n_open_nobuy=tr.get("n_open_nobuy", 0),
            n_open_stuck=tr.get("n_open_stuck", 0),
        )
        return self._json(out)

    def _tune(self, b):
        """参数寻优：对指定维度做小网格，返回排序后的组合。

        ⚠️ **两套口径，别混**（全站纪律：同一份结果被多处消费必须对齐口径）
        ------------------------------------------------------------------
        · 只勾**信号轴**（距MA60 / 市值 / 市场环境 / 波动 / ret20 / 确认）
          → 走 `stats()`，**不限仓位**口径（信号全买，实测平均同时持仓 900+ 只），
            按不限仓位 Sharpe 排序。研究报告口径。
        · 勾了**建仓节奏轴**（`rhythm`），或左侧 ⑧ 已开仓位约束（`max_pos>0` /
          `cap_ladder`）→ 每个组合额外跑 `capacity()`，改按**账户资金口径**
          评估与排序，表格也换成容量列（账户 CAGR/Sharpe、已投 CAGR、平均持仓、建仓率）。

        **为什么节奏必须走容量口径**：节奏（持几只 / 日买几只 / 阶梯）在
        「信号全买」下**完全无效** —— 没有仓位上限时，任何节奏的净值都一模一样，
        差异恒为 0。只有落到容量口径，节奏才是一个真参数。

        性能
        ----
        节奏轴让组合数×N，但容量参数**不影响 mask**：同一批信号只算一次 `stats()`
        （按信号参数指纹缓存），每个节奏变体只多跑一次 `capacity(n_sim=0)`
        （实测 0.04s）。`n_sim=200` 的随机对照要 25s/次，寻优里必须关掉 ——
        那是给单次回测做显著性检验用的，不是给网格搜索用的。
        """
        t0 = time.time()
        pool, err = self._pool(b)
        if err:
            return self._json({"error": err}, 200)
        base = dict(DEFAULT_PARAMS)
        base.update(b.get("params") or {})
        hold = int(base.get("hold") or 20)
        axes = b.get("axes") or {}
        # 每个轴的候选
        AX = {
            "px_ma60": [[1, 1], [1, 2], [1, 3], [2, 3], [1, 5], [1, 10]],
            # ⚠️ 键名必须是 "size_band"（= 前端 checkbox 的 value）。
            #    早期这里写成 "size_max"，而前端发的是 size_band →
            #    `k in AX` 判不过被静默丢弃：勾「市值区间」完全无效，
            #    只勾它还会报「请至少选择一个待寻优的维度」（实测）。
            "size_band": [[0, 0], [0, 1], [0, 2], [1, 2], [2, 4], [0, 9]],
            "mkt_state": ["bear", "any", "bull"],
            "mkt_hv": ["any", "high", "low"],
            "confirm": [[], ["histup3"], ["ma5"], ["volup"], ["ma5", "histup3"]],
            "ret20": [[None, None], [1, 3], [2, 6], [1, 1], [1, 5]],
            # ---- 建仓节奏（容量层参数，须在容量口径下评估）
            "rhythm": RHYTHM_CANDS,
            # ---- 超额选股规则（同为容量层参数）
            # ⚠️ 容量口径下「先买哪只」往往比信号条件本身更决定成败：仓位只有 N 个，
            #    而「最超跌优先」永远先从 D1 里挑 —— 所以把信号区间从 D1 放宽到
            #    D1~D10 可能**一只都多买不到**（实测账户指标完全相同，见 n_distinct_cap）。
            "pick": ["deep", "drop60", "small", "big", "amount", "rand", "none"],
        }
        # ⚠️ 默认必须是 False：前端只发「已勾选」的键（axes={key:true}），
        #    若用 axes.get(k, True)，未勾选的维度会被当成 True 一起寻优 →
        #    用户取消勾选完全无效（实测：只勾 mkt_state 仍跑了 240 个组合）。
        sel = {k: v for k, v in AX.items() if axes.get(k, False)}
        if not sel:
            return self._json({"error": "请至少选择一个待寻优的维度"}, 200)

        # 节奏轴被勾选 → 进入容量口径；左侧 ⑧ 已开约束时也跟随（与 /api/backtest 一致）
        base_lad = parse_ladder(base.get("cap_ladder"))
        has_base_cap = int(base.get("max_pos") or 0) > 0 or bool(base_lad)
        cap_mode = ("rhythm" in sel) or has_base_cap
        # ⚠️ `pick`（超额选股规则）**只在有仓位上限时才有意义**：
        #    不限仓位时信号全买，「先买哪只」根本不影响结果 —— 7 个规则会给出
        #    7 行完全相同的数字（实测）。所以单独勾它时自动补一个默认仓位，
        #    并在响应里回传 auto_cap，让前端把这件事说清楚，而不是让用户对着
        #    一排相同的数字猜。
        auto_cap = False
        if "pick" in sel and not cap_mode:
            base = dict(base, max_pos=10, max_new=int(base.get("max_new") or 3))
            auto_cap = cap_mode = True

        combos = [{}]
        for k, cands in sel.items():
            nxt = []
            for c in combos:
                for v in cands:
                    d = dict(c)
                    if k == "px_ma60":
                        d["px_ma60_min"], d["px_ma60_max"] = v
                    elif k == "ret20":
                        d["ret20_min"], d["ret20_max"] = v
                    elif k == "size_band":
                        d["size_min"], d["size_max"] = v
                    elif k == "rhythm":
                        # 节奏是一个「打包参数」：展开成 max_pos / max_new / cap_ladder。
                        # `_rhythm` 只带标签，进 p 之前会被剥掉（否则会污染参数指纹）。
                        d["max_pos"] = int(v.get("max_pos") or 0)
                        d["max_new"] = int(v.get("max_new") or 3)
                        d["cap_ladder"] = (list(v["ladder"]) if v.get("ladder") else None)
                        d["_rhythm"] = rhythm_label(v)
                    else:
                        d[k] = v
                    nxt.append(d)
            combos = nxt

        # 上限保护
        # ⚠️ 不能直接 `combos[:MAXC]`：笛卡尔积是「越靠前的轴变化越慢」生成的，
        #    硬截断会把**末尾几个轴的取值整片丢掉**（实测勾 4 个轴时被砍掉的
        #    组合里 mkt_state 只剩第一个取值，另外两个永远搜不到）。
        #    改成**等步长抽样**，让每个轴的每个取值都有机会进入结果。
        n_grid = len(combos)
        MAXC = int(b.get("max_combos") or 240)
        if n_grid > MAXC:
            step = n_grid / MAXC
            combos = [combos[int(i * step)] for i in range(MAXC)]

        rows = []
        cache = {}                      # 信号指纹 → (mask, stats)，节奏变体直接复用
        n_cap = 0
        for c in combos:
            rname = c.get("_rhythm")
            p = dict(base)
            p.update({k: v for k, v in c.items() if k != "_rhythm"})
            sig = _signal_sig(p)
            if sig in cache:
                mask, st = cache[sig]
            else:
                mask, _ = ENGINE.build_mask(p, pool)
                st = (ENGINE.stats(mask, hold) if int(mask.sum()) >= 200 else None)
                cache[sig] = (mask, st)
            if st is None or st["cagr"] is None or st["sharpe"] is None:
                continue

            label = []
            a, bb = p.get("px_ma60_min"), p.get("px_ma60_max")
            if a or bb:
                label.append(f"距MA60 D{a}~D{bb}")
            if p.get("ret20_min") or p.get("ret20_max"):
                label.append(f"ret20 D{p.get('ret20_min')}~D{p.get('ret20_max')}")
            # 市值区间：统一走 size_band_label，避免这里和 build_mask 的口径漂移
            smin, smax = norm_size_band(p.get("size_min"), p.get("size_max"))
            if smin > 0 or smax < SIZE_N - 1:
                label.append(size_band_label(smin, smax).replace("市值", ""))
            ms = p.get("mkt_state")
            if ms == "bear":
                label.append("熊市")
            elif ms == "bull":
                label.append("牛市")
            mh = p.get("mkt_hv")
            if mh == "high":
                label.append("高波动")
            elif mh == "low":
                label.append("低波动")
            cf = p.get("confirm") or []
            if cf:
                label.append("+" + "+".join(cf))
            # 选股规则：只在**非默认**时才写进标签。
            # 默认值（deep）出现在每一行里纯属噪音，反而看不清是哪一维在变。
            pk = p.get("pick")
            if pk and pk != DEFAULT_PARAMS.get("pick"):
                label.append(PICK_RULES.get(str(pk), {}).get("name", str(pk)))
            if rname:
                label.append(rname)

            row = dict(
                label=" ｜ ".join(label) if label else "全市场",
                params={k: (list(v) if isinstance(v, (list, tuple)) else v)
                        for k, v in p.items()},
                n_signal=int(mask.sum()), n_trade=st["n_trade"],
                cagr=st["cagr"], mdd=st["mdd"], sharpe=st["sharpe"],
                win=st["win"], pf=st["pf"],
                exw20=st["exw"].get(20), t20=st["exw_t"].get(20),
                oos=st["segs"].get("OOS(2021-2026)"),
                is_=st["segs"].get("IS(2015-2020)"),
                n_neg=sum(1 for y in st["yearly"] if y["ret"] < 0),
                n_year=len(st["yearly"]),
            )

            # ---- 容量口径（节奏模式）：账户资金口径才是「账户里真实看到的收益率」
            mp_c = int(p.get("max_pos") or 0)
            lad_c = parse_ladder(p.get("cap_ladder"))
            if cap_mode and (mp_c > 0 or lad_c):
                try:
                    cap = ENGINE.capacity(
                        mask, hold=hold, max_pos=mp_c,
                        max_new=int(p.get("max_new") or 3),
                        pick=str(p.get("pick") or "deep"),
                        n_sim=0,                 # ⚠️ 必须关掉：200 次随机要 25s
                        ladder=lad_c)
                except Exception:
                    import traceback
                    traceback.print_exc()
                    cap = None
                if cap:
                    n_cap += 1
                    row.update(
                        cap_cagr=cap["cap_cagr"], cap_mdd=cap["cap_mdd"],
                        cap_sharpe=cap["cap_sharpe"],
                        inv_cagr=cap["cagr"], inv_sharpe=cap["sharpe"],
                        fill_pct=cap["fill_pct"], drop_pct=cap["drop_pct"],
                        avg_pos=cap["avg_pos"],
                        # `avg_invested` / `full_pct` **两个口径都有值**：
                        #   等权 = avg_pos / max_pos（每笔仓位恒为 1/max_pos）
                        #   阶梯 = 每日权重和（来自 engine._daily_invested）
                        # ⚠️ 别用 `full_pct` 代替 `active_pct`：前者是「满仓日占比」，
                        #    后者是「有持仓日占比」，只在 avg_pos == max_pos 时才相等。
                        avg_invested=cap.get("avg_invested"),
                        max_pos=cap["max_pos"], max_new=cap["max_new"],
                        ladder=cap.get("ladder"),
                        # 分段/逐年改用**账户口径净值**（否则「账户 CAGR」配
                        # 「不限仓位 OOS」又是一处口径打架）
                        n_neg=sum(1 for y in cap["yearly"] if y["ret"] < 0),
                        n_year=len(cap["yearly"]),
                        is_=cap["segs"].get("IS(2015-2020)"),
                        oos=cap["segs"].get("OOS(2021-2026)"),
                    )
            rows.append(row)

        # 排序依据：容量模式默认按**账户资金口径 Sharpe**，否则按不限仓位 Sharpe
        sort_by = str(b.get("sort_by") or "").strip()
        allowed = (("cap_sharpe", "cap_cagr", "inv_cagr", "sharpe", "cagr")
                   if cap_mode else ("sharpe", "cagr"))
        if sort_by not in allowed:
            sort_by = allowed[0]
        rows.sort(key=lambda r: -(r.get(sort_by) if r.get(sort_by) is not None else -9))
        n_distinct_cap = max_same_cap = None
        if cap_mode:
            # 容量口径下**有多少行其实给出了同一个结果** —— 用于前端解释
            # 「为什么好几行数字一模一样」。成因：仓位上限只有 N 个，而选股规则
            # （如「最超跌优先」）总是先从最窄的那一档里挑满，放宽嵌套区间多买不到。
            seen = {}
            for r in rows:
                if r.get("cap_cagr") is None:
                    continue
                k = (round(r["cap_cagr"], 6), round(r["cap_sharpe"], 6))
                seen[k] = seen.get(k, 0) + 1
            n_distinct_cap = len(seen)
            max_same_cap = max(seen.values()) if seen else 0
        return self._json(dict(ms=int((time.time() - t0) * 1000),
                               n_combo=len(rows), n_grid=n_grid, n_used=len(combos),
                               n_cap=n_cap, n_distinct_cap=n_distinct_cap,
                               max_same_cap=max_same_cap,
                               cap_mode=cap_mode, auto_cap=auto_cap, sort_by=sort_by,
                               rows=rows[:120]))

    # ------------------------------------------------------------ 我的方案
    def _preset_save(self, b):
        """保存当前参数为自定义方案。

        请求体： name（必填，同名即覆盖）、params（必填）、
                 pool（可选，股票池：板块/行业/自定义代码/交易所）、desc（可选）
        返回：   ok / id / name / presets（保存后的全量列表，前端直接重绘）

        ⚠️ pool 必须单独接收 —— 它不在 params 白名单里，早期版本漏收，
           导致整套「股票池」筛选存不下来。
        ⚠️ 失败一律用 `ok:false + msg`，**不要**用 `error` 键：
           前端的 api() 见到 error 就直接 throw，那样「名称为空」这种
           可预期的表单错误会变成异常，前端拿不到干净的提示。
        """
        name = str(b.get("name") or "").strip()
        if not name:
            return self._json({"ok": False, "msg": "请先给这套参数起个名"}, 200)
        if len(name) > PRESET_NAME_MAX:
            return self._json(
                {"ok": False, "msg": f"名称最长 {PRESET_NAME_MAX} 个字符"}, 200)
        params = clean_params(b.get("params"))
        if not params:
            return self._json({"ok": False, "msg": "参数为空，无法保存"}, 200)
        pool = clean_pool(b.get("pool"))
        existed = any(x["name"] == name for x in load_user_presets())
        hit, lst = upsert_user_preset(name, params, str(b.get("desc") or ""), pool)
        return self._json({"ok": True, "id": hit["id"], "name": hit["name"],
                           "overwrote": existed,
                           "msg": (f"已覆盖同名方案「{name}」" if existed
                                   else f"已保存方案「{name}」"),
                           "presets": lst})

    def _preset_delete(self, b):
        pid = str(b.get("id") or "")
        if not pid:
            return self._json({"ok": False, "msg": "缺少 id"}, 200)
        if not any(x["id"] == pid for x in load_user_presets()):
            return self._json({"ok": False, "msg": "该方案不存在"}, 200)
        lst = delete_user_preset(pid)
        # ⚠️ 删掉的恰好是当前默认方案 → 一并清掉默认设置，否则下次启动会
        #    指向一个不存在的 id，前端只能静默回退（用户会以为「设置没保存住」）。
        if load_default_preset() == pid:
            save_default_preset(None)
        return self._json({"ok": True, "msg": "已删除", "presets": lst,
                           "default_preset": load_default_preset()})

    def _preset_set_default(self, b):
        """设置「界面默认预设」。

        请求体： id —— 方案 id（B* 内置 / U* 我的）；**传空字符串 = 取消设置**
                （恢复成「内置第一套」）。
        返回：   ok / msg / default_preset（设置后的值，前端据此重绘标记）
        """
        pid = str(b.get("id") or "").strip()
        if pid and not preset_exists(pid):
            return self._json({"ok": False, "msg": "该方案不存在"}, 200)
        save_default_preset(pid or None)
        cur = load_default_preset()
        name = ""
        for x in list(PRESETS) + load_user_presets():
            if x.get("id") == cur:
                name = x.get("name") or ""
                break
        msg = (f"已设为默认：{name}" if cur
               else "已取消默认设置（将使用内置第一套）")
        return self._json({"ok": True, "msg": msg, "default_preset": cur})

    # ------------------------------------------------------------ 实盘持仓
    def _positions(self, b):
        """当前持仓：当前参数下「已买入、持有期还没走完」的头寸（**自动**，无需录入）。

        与 /api/holdings 的分工（⚠️ 别混）
        --------------------------------
        · 本接口 = **策略让你现在拿着什么** —— 随参数变化，只有**收益率没有金额**
          （回测里没有「你投了多少钱」，硬编一个股数出来就是编数据）
        · /api/holdings = **你真的买了什么** —— 手动账本，有成本/股数，算金额盈亏
        · 价格口径也不同：本接口用**复权**价（与回测 buy 同尺度），账本用**未复权**价
          （与券商成交价同尺度）。两张表的合计**绝不能相加**。

        请求体：`{params, pool}`（同 /api/backtest）。
        """
        t0 = time.time()
        pool, err = self._pool(b)
        if err:
            return self._json({"error": err}, 200)
        p = dict(DEFAULT_PARAMS)
        p.update(b.get("params") or {})
        hold = int(p.get("hold") or 20)
        mask, _conds = ENGINE.build_mask(p, pool)
        at = str(ENGINE.day_str[ENGINE.nd - 1])
        if int(mask.sum()) == 0:
            return self._json(dict(ok=True, at=at, hold=hold, rows=[], totals=None,
                                   msg="当前条件下没有任何信号"), 200)
        # ---- 容量约束（与 /api/trades 同口径）：只有开了仓位上限才过滤
        mp = int(p.get("max_pos") or 0)
        lad = parse_ladder(p.get("cap_ladder"))
        plan = None
        if mp > 0 or lad:
            try:
                plan = ENGINE.capacity_plan(
                    mask, hold=hold, max_pos=mp,
                    max_new=int(p.get("max_new") or 3),
                    pick=str(p.get("pick") or "deep"), ladder=lad)
            except Exception:
                import traceback
                traceback.print_exc()
                plan = None
        rows = ENGINE.positions(mask, hold=hold, plan=plan)
        # ---- 汇总：`pending`（还没到买入日）**不算持仓**，单列。
        #   实测恒为 0：T+1 开盘价取不到时该行会被归进 nobuy，进不了 holding（见 engine.positions）。
        held = [x for x in rows if not x["pending"]]
        vals = [x["pnl_pct"] for x in held if x["pnl_pct"] is not None]
        tot = dict(
            n=len(rows), n_hold=len(held), n_pending=len(rows) - len(held),
            n_stock=len({x["code"] for x in held}),
            n_win=sum(1 for v in vals if v > 0),
            n_loss=sum(1 for v in vals if v < 0),
            avg_pct=(round(float(np.mean(vals)), 6) if vals else None),
            med_pct=(round(float(np.median(vals)), 6) if vals else None),
            best=(round(float(max(vals)), 6) if vals else None),
            worst=(round(float(min(vals)), 6) if vals else None),
            next_exit=(held[0]["exit_est"] if held else None),
            hold=hold, capped=bool(plan),
        )
        return self._json(dict(ok=True, at=at, hold=hold, rows=rows, totals=tot,
                               ms=int((time.time() - t0) * 1000)))

    def _holdings_get(self):
        """列出持仓账本 + 按最新行情现算的浮动盈亏。

        ⚠️ 盈亏**不落盘**：每次请求都重算，行情更新后不会残留旧数字。
        ⚠️ 无行情的条目（退市 / ST 被面板剔除 / 代码打错）**不计入合计**，
           但必须单独报出来（`n_noquote` + `noquote`）—— 不能悄悄消失，
           否则用户会以为「我明明录了 10 只，怎么只显示 8 只」。
        """
        lots = load_holdings()
        q = ENGINE.quotes([x["code"] for x in lots]) if lots else {}
        # ⚠️ 直接从引擎取面板末日，**不要**用 ENGINE.meta()[last_date]：
        #    meta() 每次都会重建成千上万只票的清单 + 行业树，持仓页刷新一次就白烧一遍。
        asof = str(ENGINE.day_str[ENGINE.nd - 1])
        rows = []
        agg = dict(cost=0.0, mv=0.0, pnl=0.0, day=0.0, n_px=0, n_win=0, n_loss=0)
        noquote = []
        for x in lots:
            key = ENGINE._norm_code(x["code"])
            qi = q.get(key)
            shares, cost = float(x["shares"]), float(x["cost"])
            cost_amt = cost * shares
            row = dict(id=x["id"], code=x["code"], name=x["name"],
                       cost=round(cost, 4), shares=shares, cost_amt=round(cost_amt, 2),
                       buy_date=x["buy_date"] or None, note=x["note"],
                       ind="", board="", price=None, price_date=None, chg=None,
                       stale_days=None, price_kind=None, mv=None, pnl=None,
                       pnl_pct=None, day_pnl=None, hold_days=None, noquote=True)
            if qi is not None:
                price, prev = qi["price"], qi["prev"]
                mv = price * shares
                pnl = mv - cost_amt
                day_pnl = (price - prev) * shares if prev else None
                row.update(
                    code=qi["code"], name=(x["name"] or qi["name"]),
                    ind=qi["ind"], board=qi["board"],
                    price=price, price_date=qi["date"], chg=qi["chg"],
                    stale_days=qi["stale_days"], price_kind=qi["px_kind"],
                    mv=round(mv, 2), pnl=round(pnl, 2),
                    pnl_pct=(round(pnl / cost_amt, 6) if cost_amt > 0 else None),
                    day_pnl=(round(day_pnl, 2) if day_pnl is not None else None),
                    hold_days=_hold_days(x["buy_date"], qi["date"]),
                    noquote=False)
                agg["cost"] += cost_amt
                agg["mv"] += mv
                agg["pnl"] += pnl
                if day_pnl is not None:
                    agg["day"] += day_pnl
                agg["n_px"] += 1
                if pnl > 0:
                    agg["n_win"] += 1
                elif pnl < 0:
                    agg["n_loss"] += 1
            else:
                noquote.append(x["code"])
            rows.append(row)
        # 排序：有行情的按浮动盈亏率降序在前，无行情的沉底（一眼看到赚最多/亏最多）
        rows.sort(key=lambda r: (r["noquote"],
                                 -(r["pnl_pct"] if r["pnl_pct"] is not None else 0.0)))
        base = agg["mv"] - agg["day"]
        tot = dict(
            n_lot=len(rows),
            n_stock=len({ENGINE._norm_code(r["code"]) for r in rows}),
            n_priced=agg["n_px"], n_win=agg["n_win"], n_loss=agg["n_loss"],
            cost=round(agg["cost"], 2), mv=round(agg["mv"], 2),
            pnl=round(agg["pnl"], 2),
            pnl_pct=(round(agg["pnl"] / agg["cost"], 6) if agg["cost"] > 0 else None),
            day_pnl=round(agg["day"], 2),
            day_base=round(base, 2),
            day_pct=(round(agg["day"] / base, 6) if base > 0 else None),
        )
        return self._json(dict(ok=True, at=asof, rows=rows, totals=tot,
                               n_noquote=len(noquote), noquote=noquote))

    def _holding_save(self, b):
        """新增 / 修改一个持仓批次。

        请求体： id（有 = 修改，无 = 新增）、code、cost、shares、
                 buy_date（YYYY-MM-DD，可空）、note（可空）
        返回：   ok / msg / id

        ⚠️ 校验失败一律 `ok:false + msg`，**不用** `error` 键：前端 api() 见到
           `error` 会直接 throw，那样「成本价填成 0」这种表单错误就变成异常，
           提示框里只剩一句没头没尾的报错。
        """
        key = ENGINE._norm_code(b.get("code"))
        if not key:
            return self._json({"ok": False, "msg": "请填写 6 位股票代码，如 600000"}, 200)
        try:
            cost = float(b.get("cost"))
        except (TypeError, ValueError):
            return self._json({"ok": False, "msg": "成本价必须是数字"}, 200)
        if not (cost > 0):
            return self._json({"ok": False, "msg": "成本价必须大于 0"}, 200)
        try:
            shares = float(b.get("shares"))
        except (TypeError, ValueError):
            return self._json({"ok": False, "msg": "股数必须是数字"}, 200)
        if not (shares > 0):
            return self._json({"ok": False, "msg": "股数必须大于 0"}, 200)
        buy_date = str(b.get("buy_date") or "").strip()[:10]
        if buy_date:
            try:
                date.fromisoformat(buy_date)
            except ValueError:
                return self._json({"ok": False, "msg": "买入日格式应为 YYYY-MM-DD"}, 200)
        # 名称优先取面板里的官方简称（防用户手打错别字），面板没有就留空
        q = ENGINE.quotes([key]).get(key) or {}
        name = str(b.get("name") or "").strip() or str(q.get("name") or "")
        note = str(b.get("note") or "").strip()[:HOLD_NOTE_MAX]

        lst = load_holdings()
        pid = str(b.get("id") or "").strip()
        rec = dict(id=pid or _new_hold_id(lst), code=(q.get("code") or key),
                   name=name, cost=round(cost, 4), shares=shares,
                   buy_date=buy_date, note=note,
                   saved_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        if pid:
            for i, x in enumerate(lst):
                if x["id"] == pid:
                    lst[i] = rec
                    break
            else:
                return self._json({"ok": False, "msg": "该持仓记录已不存在，请刷新"}, 200)
            msg = "已更新这笔持仓"
        else:
            if len(lst) >= HOLD_LOTS_MAX:
                return self._json(
                    {"ok": False, "msg": f"最多 {HOLD_LOTS_MAX} 笔，请先删掉不用的"}, 200)
            lst.append(rec)
            msg = f"已添加 {name or rec['code']}"
        save_holdings(lst)
        if not q:
            msg += "（该代码不在面板内，暂无行情，暂不计入合计）"
        return self._json({"ok": True, "msg": msg, "id": rec["id"]})

    def _holding_delete(self, b):
        pid = str(b.get("id") or "").strip()
        if not pid:
            return self._json({"ok": False, "msg": "缺少 id"}, 200)
        lst = load_holdings()
        keep = [x for x in lst if x["id"] != pid]
        if len(keep) == len(lst):
            return self._json({"ok": False, "msg": "该持仓记录已不存在，请刷新"}, 200)
        save_holdings(keep)
        return self._json({"ok": True, "msg": "已删除"})

    def _export(self, b):
        # 两种导出：candidates（今日候选股） / trades（回测逐笔明细）
        kind = (b.get("kind") or "candidates").lower()
        if kind == "trades":
            return self._export_trades(b)
        pool, err = self._pool(b)
        if err:
            return self._json({"error": err}, 200)
        p = dict(DEFAULT_PARAMS)
        p.update(b.get("params") or {})
        res = ENGINE.scan(pool_mask=pool, limit=int(b.get("limit") or 500), p=p)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["日期", "代码", "名称", "行业", "交易所", "收盘价",
                    "距MA60%", "距MA20%", "ret20%", "ret60%",
                    "距MA60分位", "ret40分位", "ret60分位", "市值分组(0=最小)",
                    "HV20%", "量比",
                    "营收(元)", "营收同比%", "归母净利(元)", "归母同比%",
                    "财报期", "财报季度",
                    "打分", "次日可买"])
        for it in res.get("items", []):
            w.writerow([res.get("asof") or res.get("date"), it["code"], it["name"],
                        it["ind"], it["ex"],
                        it["close"], it["dist60"], it["dist20"], it["ret20"],
                        it["ret60"], it["d_px60"], it["d_ret40"], it["d_ret60"],
                        it["size_grp"], it["hv20"], it["rvol"],
                        it.get("fin_rev"), it.get("fin_rev_yoy"),
                        it.get("fin_np"), it.get("fin_np_yoy"),
                        it.get("fin_end"), it.get("fin_q"),
                        it["score"],
                        "是" if it["can_buy"] else "否"])
        data = "\ufeff" + buf.getvalue()
        body = data.encode("utf-8")

        # ⚠️ Content-Disposition 含非 ASCII 文件名时，http.server 会用 latin-1
        #    编码 header → 抛 UnicodeEncodeError → 响应头未发出即断连（表现为 0 字节）。
        #    必须按 RFC 5987 做百分号编码，并同时给一个纯 ASCII 的 filename 兜底。
        asof = res.get("asof") or res.get("date") or "latest"
        ascii_fn = f"candidates_{asof}.csv"
        utf8_fn = quote(f"超跌反转候选股_{asof}.csv", safe="")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition",
                         f"attachment; filename=\"{ascii_fn}\"; "
                         f"filename*=UTF-8''{utf8_fn}")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _export_trades(self, b):
        """导出回测逐笔交易明细 CSV（含筛选条件）。"""
        pool, err = self._pool(b)
        if err:
            return self._json({"error": err}, 200)
        p = dict(DEFAULT_PARAMS)
        p.update(b.get("params") or {})
        hold = int(p.get("hold") or 20)
        mask, _ = ENGINE.build_mask(p, pool)
        # ---- 容量约束：口径必须与页面一致，否则导出 CSV ≠ 屏幕所见
        mp = int(p.get("max_pos") or 0)
        plan = None
        if mp > 0:
            plan = ENGINE.capacity_plan(
                mask, hold=hold, max_pos=mp,
                max_new=int(p.get("max_new") or 3),
                pick=str(p.get("pick") or "deep"))
        tr = ENGINE.trades(mask, hold, cost=float(b.get("cost", 0.003)), plan=plan)
        if tr is None:
            return self._json({"error": "该参数下没有完成的交易"}, 200)
        rows = tr["rows"]
        win_f = b.get("win")
        if win_f is not None:
            rows = [r for r in rows if r["win"] == bool(win_f)]
        yr = b.get("year")
        if yr:
            rows = [r for r in rows
                    if r["entry_date"] and r["entry_date"][:4] == str(int(yr))]

        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["序号", "代码", "名称", "行业", "交易所", "信号日", "买入日",
                    "卖出日", "持有天数", "买入价", "卖出价",
                    "净收益%", "毛收益%", "同期基准%", "超额%", "盈亏",
                    "市值分组(0=最小)", "距MA60分位", "ret60分位",
                    "营收(元)", "营收同比%", "归母净利(元)", "归母同比%",
                    "财报期", "财报季度"])
        for r in rows:
            w.writerow([r["seq"], r["code"], r["name"], r["ind"], r["ex"],
                        r["signal_date"], r["entry_date"], r["exit_date"],
                        r["hold"], r["buy"], r["sell"],
                        r["net"], r["ret"], r["bench"], r["excess"],
                        "盈" if r["win"] else "亏",
                        r["size_grp"], r["d_px60"], r["d_ret60"],
                        r["fin_rev"], r["fin_rev_yoy"],
                        r["fin_np"], r["fin_np_yoy"],
                        r["fin_end"], r["fin_q"]])
        # ---- 未结束交易（⚠️ 必须与屏幕一致：屏幕有这一段，CSV 却漏掉的话，
        #      「导出后怎么少了几笔」又是一个对不上的数字）
        #      用「未结束」标记 + 已持有/剩余天数；收益列一律留空（没有结局）。
        #       ⚠️ 三种 kind 都要有明确文案：holding / nobuy / stuck
        #          （stuck = 买进了但卖出日无价，算不出收益）
        _KIND_CN = {"holding": "持有中", "nobuy": "T+1 买不进", "stuck": "卖出日无价"}
        for r in (tr.get("open_rows") or []):
            w.writerow(["未结束", r["code"], r["name"], r["ind"], r["ex"],
                        r["signal_date"], r["entry_date"], "",
                        f'{r["hold_days"]}/{r["hold"]}', r["buy"], "",
                        "", "", "", "",
                        _KIND_CN.get(r["kind"], r["kind"]),
                        r["size_grp"], r["d_px60"], r["d_ret60"],
                        "", "", "", "", "", ""])
        data = "\ufeff" + buf.getvalue()
        body = data.encode("utf-8")
        # RFC 5987（同 _export，避免 latin-1 编码 header 崩溃）
        tag = f"H{hold}"
        if plan:
            tag += f"_cap{mp}x{int(p.get('max_new') or 3)}"
        if yr:
            tag += f"_{int(yr)}"
        if win_f is not None:
            tag += "_win" if win_f else "_loss"
        ascii_fn = f"trades_{tag}.csv"
        utf8_fn = quote(f"超跌反转交易明细_{tag}.csv", safe="")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition",
                         f"attachment; filename=\"{ascii_fn}\"; "
                         f"filename*=UTF-8''{utf8_fn}")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------ 静态
    def _serve_file(self, name, ctype):
        path = os.path.realpath(os.path.join(APP, name))
        # 目录穿越防护：/static/..%2f..%2fxxx 这类请求必须挡在 APP 之外
        if not path.startswith(os.path.realpath(APP) + os.sep):
            return self._json({"error": "forbidden"}, 403)
        if not os.path.isfile(path):
            return self._json({"error": f"{name} not found"}, 404)
        key = (name, os.path.getmtime(path))
        if key not in _html_cache:
            # 先清掉同名旧版本，否则每次改文件都会留一份缓存键，长期只增不减
            for k in [k for k in _html_cache if k[0] == name]:
                _html_cache.pop(k, None)
            with open(path, "rb") as f:
                _html_cache[key] = f.read()
        body = _html_cache[key]
        if ctype is None:
            ctype = _STATIC_CTYPES.get(os.path.splitext(name)[1].lower(),
                                       "application/octet-stream")
        self._send(200, body, ctype)


PRESETS = [
    dict(id="K3", name="★ 最优 3 条件（距MA60 D1 + 小市值30% + 熊市）",
         desc="研究报告第12节「最简单的有效策略」。CAGR 29.9% / MDD −41.3% / Sharpe 0.98 / 正年份 10/12",
         params=dict(DEFAULT_PARAMS)),
    dict(id="K2", name="K2 距MA60 D1 + 小市值30%",
         desc="去掉熊市过滤，交易机会更多但回撤更大。CAGR 28.0% / MDD −46.9% / Sharpe 0.90",
         params=dict(px_ma60_min=1, px_ma60_max=1, size_max=2,
                     mkt_state="any", mkt_hv="any", deep_any=False,
                     confirm=[], hold=20)),
    dict(id="K1", name="K1 距MA60 D1（单条件）",
         desc="完全不筛选市值与市场环境。CAGR 14.7% / MDD −53.4% / Sharpe 0.58",
         params=dict(px_ma60_min=1, px_ma60_max=1, size_max=9,
                     mkt_state="any", mkt_hv="any", deep_any=False,
                     confirm=[], hold=20)),
    dict(id="K12", name="K12 距MA120 D1 + 小市值30% + 熊市",
         desc="用更长期的 MA120 衡量超跌，回撤略小。CAGR 24.5% / MDD −39.3% / Sharpe 0.86",
         params=dict(px_ma120_min=1, px_ma120_max=1, size_max=2,
                     mkt_state="bear", mkt_hv="any", deep_any=False,
                     confirm=[], hold=20)),
    dict(id="K11", name="K11 ret20 D1~D3 + 小市值30% + 熊市",
         desc="用 20 日跌幅衡量超跌，信号更密集。CAGR 24.6% / MDD −42.8% / Sharpe 0.90",
         params=dict(ret20_min=1, ret20_max=3, size_max=2,
                     mkt_state="bear", mkt_hv="any", deep_any=False,
                     confirm=[], hold=20)),
    dict(id="D2D6", name="用户原设想：ret20 D2~D6 + 小市值30% + 熊市",
         desc="研究报告已证实：中等超跌 + 等确认 不如极端超跌直接买。仅供对照验证",
         params=dict(ret20_min=2, ret20_max=6, size_max=2,
                     mkt_state="bear", mkt_hv="any", deep_any=False,
                     confirm=[], hold=20)),
    # ---- 财务增强（动态 as-of，回测无前视偏差）
    dict(id="K3G", name="K3 + 营收同比>0（剔除负增长）",
         desc="在 K3 基础上只买营收仍在增长的公司。信号 56,185；CAGR 25.8% / MDD −40.4% / Sharpe 0.88",
         params=dict(DEFAULT_PARAMS, rev_yoy_min=0.0)),
    dict(id="K3G20", name="K3 + 营收同比>20%（成长股）",
         desc="营收高增长 + 超跌。信号 25,941；CAGR 26.4% / MDD −42.1% / Sharpe 0.89",
         params=dict(DEFAULT_PARAMS, rev_yoy_min=0.2)),
    dict(id="K3P", name="K3 + 归母净利润同比>0（盈利改善）",
         desc="只买利润仍在增长的公司。信号 38,680；CAGR 28.4% / MDD −39.8% / Sharpe 0.96",
         params=dict(DEFAULT_PARAMS, np_yoy_min=0.0)),
    dict(id="K3BIG", name="K3 + 营收>10亿（剔除微盘）",
         desc="加营收门槛过滤掉空壳/微盘股。信号 18,485；CAGR 21.0% / MDD −43.2% / Sharpe 0.76",
         params=dict(DEFAULT_PARAMS, rev_min=1e9)),
    dict(id="K3Q", name="K3 + 营收>10亿 + 归母同比>0（稳健）",
         desc="基本面双确认：有规模 + 利润改善。信号 7,406；CAGR 21.2% / MDD −46.4% / Sharpe 0.75",
         params=dict(DEFAULT_PARAMS, rev_min=1e9, np_yoy_min=0.0)),
]


# ================================================================ 实盘持仓账本
# 手动录入的持仓，落到 data/holdings.json。**一条记录 = 一个买入批次（lot）**，
# 同一只票可以有多条 —— 分批建仓各有自己的成本价与买入日，分别算盈亏再汇总。
#
# ⚠️ 为什么不做「一个代码一条记录」：那样得分批合并成本（加权/先进先出），
#    口径一旦写死就没法解释；按批次存、逐行列出，信息量更大也不用猜。
# ⚠️ 盈亏一律在**读的时候**用最新行情现算，**绝不落盘**。落盘的只有用户输入
#    （代码/成本/股数/买入日/备注）—— 否则行情一更新，文件里的旧盈亏就在说谎。
HOLDING_FILE = os.path.join(ROOT, "data", "holdings.json")
HOLD_LOTS_MAX = 500          # 单账户最多 500 个批次：够用，也能挡住手滑批量导入
HOLD_NOTE_MAX = 60


def _hold_days(buy, ref):
    """买入日 → 参考日（该票最新行情日）的自然日数；日期缺失/非法返回 None。

    ⚠️ 用**自然日**而非交易日：账本只关心「买了多久」，为此去查交易日历不值得；
       界面标注写成「持有(天)」并在列头 tooltip 里说明是自然日，避免误读。
    """
    try:
        a = date.fromisoformat(str(buy)[:10])
        b = date.fromisoformat(str(ref)[:10])
    except (TypeError, ValueError):
        return None
    return max(0, (b - a).days)


def load_holdings():
    """读持仓账本；文件缺失/损坏一律当空账本（绝不阻塞应用启动）。"""
    try:
        with open(HOLDING_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        return []
    except Exception as e:
        print(f"  {mark('⚠️', '[!]')} holdings.json 读取失败（忽略）：{e}", flush=True)
        return []
    if not isinstance(d, list):
        return []
    out = []
    for x in d:                       # 手改坏的条目直接跳过，别让它污染整个账本
        if not isinstance(x, dict):
            continue
        try:
            cost = float(x.get("cost"))
            shares = float(x.get("shares"))
        except (TypeError, ValueError):
            continue
        code = str(x.get("code") or "").strip().upper()
        if not code or not (cost > 0) or not (shares > 0):
            continue
        out.append(dict(
            id=str(x.get("id") or f"H{len(out) + 1}"),
            code=code,
            name=str(x.get("name") or ""),
            cost=round(cost, 4),
            shares=shares,
            buy_date=str(x.get("buy_date") or "")[:10],
            note=str(x.get("note") or "")[:HOLD_NOTE_MAX],
            saved_at=str(x.get("saved_at") or ""),
        ))
    return out[:HOLD_LOTS_MAX]


def save_holdings(lst):
    """原子写（同 user_presets.json）：先写 .tmp 再 os.replace，避免半截文件。"""
    os.makedirs(os.path.dirname(HOLDING_FILE), exist_ok=True)
    tmp = HOLDING_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(lst[:HOLD_LOTS_MAX], f, ensure_ascii=False, indent=2)
    os.replace(tmp, HOLDING_FILE)


def _new_hold_id(lst):
    """分配下一个 H 编号（删掉的编号不回收，避免历史引用串号）。"""
    used = {x["id"] for x in lst}
    i = 1
    while f"H{i}" in used:
        i += 1
    return f"H{i}"


# ================================================================ 我的方案
# 用户在前端调好参数后「保存为自定义方案」，落到 data/user_presets.json。
#
# ⚠️ 为什么不放 localStorage：这是本地服务，方案应该跟着**数据目录**走 ——
#    换浏览器 / 清缓存不该丢，也应该能被脚本（make_fixtures / 回测复现）读到。
# ⚠️ 何不用「名称当主键」：名称可以中文、含空格、随时改；用稳定的 U 编号做 id，
#    名称只用于「同名即覆盖」的判定与展示。
USER_PRESET_FILE = os.path.join(ROOT, "data", "user_presets.json")

# 允许被保存的参数键（白名单）。
# 只收 build_mask / capacity / plan_positions 认识的键，拒绝一切杂项，
# 免得前端手滑把 UI 状态（比如当前 Tab）也存进方案里。
PRESET_PARAM_KEYS = {
    "px_ma60_min", "px_ma60_max", "px_ma120_min", "px_ma120_max",
    "ret20_min", "ret20_max", "ret60_min", "ret60_max", "deep_any",
    "size_min", "size_max",
    "mkt_state", "mkt_hv", "breadth_min", "breadth_max",
    "rs_sz_min", "rs_sz_max", "hv_min", "hv_max", "rvol_min", "rvol_max",
    "confirm", "hold",
    "max_pos", "max_new", "pick", "cap_ladder",
    "rev_min", "rev_max", "rev_yoy_min", "rev_yoy_max",
    "np_min", "np_max", "np_yoy_min", "np_yoy_max", "fin_period",
    "delist", "delist_rev_floor", "delist_main_2024", "delist_penny_days",
}

# 「股票池」不在 params 里 —— 它走请求体独立的 pool 字段（build_pool 的 spec）。
# ⚠️ 这里必须单列一份白名单：早期版本保存预设时只存了 params，
#    于是**整个股票池组都没存下来**（板块 / 行业 / 自定义代码 / 交易所），
#    表现为「保存后切走再切回来，板块筛选丢了」。
POOL_KEYS = ("mode", "codes_text", "industries", "boards", "exchanges")
POOL_MODES = ("all", "custom")
POOL_CODES_MAX = 20000       # 代码清单可以很长，不能用 _jsonable 的 400 字符限制
POOL_LIST_MAX = 500          # 行业树细分条目数上限（面板约 100+，留足余量）

PRESET_NAME_MAX = 40


def _jsonable(v, depth=0):
    """递归判断值能否安全落盘（拒绝 NaN / 自定义对象 / 过深嵌套）。"""
    if depth > 6:
        return False
    if v is None or isinstance(v, bool):
        return True
    if isinstance(v, str):
        return len(v) <= 400
    if isinstance(v, (int, float)):
        return not (isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))))
    if isinstance(v, (list, tuple)):
        return len(v) <= 200 and all(_jsonable(x, depth + 1) for x in v)
    if isinstance(v, dict):
        return len(v) <= 60 and all(
            isinstance(k, str) and _jsonable(x, depth + 1) for k, x in v.items())
    return False


def clean_params(raw):
    """把前端提交的参数字典洗成可安全保存/复用的子集。"""
    if not isinstance(raw, dict):
        return None
    out = {}
    for k, v in raw.items():
        if k not in PRESET_PARAM_KEYS or not _jsonable(v):
            continue
        out[k] = list(v) if isinstance(v, tuple) else v
    return out or None


def clean_pool(raw):
    """把前端提交的「股票池」洗成可安全保存/复用的子集。

    和 clean_params 平行：股票池走的是请求体的 pool 字段，键名与 build_pool
    的 spec 一致。返回 None 表示「这份方案没有记录股票池」—— 前端据此
    **保持页面现有池子不动**（内置预设与早期方案都没有 pool，行为不变）。
    """
    if not isinstance(raw, dict):
        return None
    mode = str(raw.get("mode") or "all").strip().lower()
    txt = raw.get("codes_text")

    def _strlist(key, cap):
        v = raw.get(key)
        if not isinstance(v, (list, tuple)):
            return []
        seen, res = set(), []
        for x in v:
            s = str(x).strip()
            if not s or s in seen:
                continue
            seen.add(s)
            res.append(s[:80])
            if len(res) >= cap:
                break
        return res

    return {
        "mode": mode if mode in POOL_MODES else "all",
        "codes_text": txt[:POOL_CODES_MAX] if isinstance(txt, str) else "",
        "industries": _strlist("industries", POOL_LIST_MAX),
        # 板块 / 交易所取值固定为大写代码，统一归一，免得 'main' 存进去读不出来
        "boards": [b.upper() for b in _strlist("boards", 8)],
        "exchanges": [e.upper() for e in _strlist("exchanges", 8)],
    }


def load_user_presets():
    try:
        with open(USER_PRESET_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        return []
    except Exception as e:
        print(f"  {mark('⚠️', '[!]')} user_presets.json 读取失败（忽略）：{e}", flush=True)
        return []
    if not isinstance(d, list):
        return []
    # 读的时候也做一次清洗：文件可能被手改坏
    out = []
    for x in d:
        if not isinstance(x, dict):
            continue
        nm = str(x.get("name") or "").strip()
        ps = clean_params(x.get("params"))
        if not nm or not ps:
            continue
        # 「pool」键缺失（内置预设形态 / 早期保存的方案）→ None，
        # 前端据此保持页面现有股票池不动，行为与老版本一致。
        po = clean_pool(x.get("pool")) if "pool" in x else None
        out.append(dict(id=str(x.get("id") or f"U{len(out)+1}"),
                        name=nm[:PRESET_NAME_MAX],
                        desc=str(x.get("desc") or ""),
                        saved_at=str(x.get("saved_at") or ""),
                        params=ps, pool=po))
    return out


def save_user_presets(lst):
    """原子写：先写 .tmp 再 os.replace，避免写到一半进程被杀导致文件半截。"""
    os.makedirs(os.path.dirname(USER_PRESET_FILE), exist_ok=True)
    tmp = USER_PRESET_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(lst, f, ensure_ascii=False, indent=2)
    os.replace(tmp, USER_PRESET_FILE)


def upsert_user_preset(name, params, desc="", pool=None):
    """同名覆盖（保留原 id），否则追加一个新 U 编号。返回 (记录, 全量列表)。"""
    name = (name or "").strip()[:PRESET_NAME_MAX]
    lst = load_user_presets()
    hit = next((x for x in lst if x["name"] == name), None)
    if hit is None:
        used = {x["id"] for x in lst}
        i = 1
        while f"U{i}" in used:
            i += 1
        hit = dict(id=f"U{i}", name=name, desc=desc or "", saved_at="",
                   params=params, pool=pool)
        lst.append(hit)
    else:
        hit["params"] = params
        # pool=None 表示「本次没提交股票池」（脚本直连 API 的形态）→ 保留原值；
        # 前端永远会发一份完整 pool，所以想清空也清得掉。
        if pool is not None:
            hit["pool"] = pool
        if desc:
            hit["desc"] = desc
    hit["saved_at"] = time.strftime("%Y-%m-%d %H:%M")
    save_user_presets(lst)
    return hit, lst


def delete_user_preset(pid):
    lst = load_user_presets()
    rest = [x for x in lst if x["id"] != pid]
    if len(rest) != len(lst):
        save_user_presets(rest)
    return rest


# ================================================================ 默认预设
# 「界面打开时默认选中哪套方案」。
#
# ⚠️ 为什么不塞进 user_presets.json：那个文件是**方案列表**（JSON 数组），
#    往里加元字段会破坏结构，且删除某个方案时容易把元数据一起清掉。
#    单独一个文本文件最干净，也便于人工查看/修改。
# ⚠️ 为什么不用 localStorage：与 user_presets 同理 —— 本地服务应该让配置
#    跟着**数据目录**走；换浏览器 / 清缓存不该丢，脚本也要能读到。
# ⚠️ 存的是**方案 id**（B* 内置 / U* 我的），不是名称 —— 名称可以改。
DEFAULT_PRESET_FILE = os.path.join(ROOT, "data", "default_preset.txt")


def load_default_preset() -> str:
    """读默认预设 id。文件不存在/空 → 返回 ''（前端回退到内置第一套）。"""
    try:
        with open(DEFAULT_PRESET_FILE, encoding="utf-8-sig") as f:
            return (f.readline() or "").strip()
    except OSError:
        return ""


def save_default_preset(pid: str | None) -> None:
    """写默认预设 id；pid 为空 → 删除文件（= 恢复「内置第一套」）。原子写。"""
    if not pid:
        try:
            os.remove(DEFAULT_PRESET_FILE)
        except OSError:
            pass
        return
    os.makedirs(os.path.dirname(DEFAULT_PRESET_FILE), exist_ok=True)
    tmp = DEFAULT_PRESET_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(str(pid).strip() + "\n")
    os.replace(tmp, DEFAULT_PRESET_FILE)


def preset_exists(pid: str) -> bool:
    """该 id 是否是有效方案（内置 PRESETS 或「我的方案」之一）。"""
    if not pid:
        return False
    if any(x.get("id") == pid for x in PRESETS):
        return True
    return any(x.get("id") == pid for x in load_user_presets())


# ---------------------------------------------------------------- 数据更新
# 界面上的「更新数据」按钮 → 后端拉起一次 `ctl.py update`。
#
# ⚠️ 这里有个**结构性矛盾**必须绕开：
#   更新面板（02/21 号脚本）要独占约 7GB 内存，所以必须先**停服**再重建。
#   而「停服」要停的正是**当前正在处理这个 HTTP 请求的进程**——
#   天真做法（fork 一个线程去 `ctl.py update`）会先把自己杀掉，更新流程
#   随进程组一起死掉，表现为「点了更新、服务停了、数据没更新、也不再起来」。
#
# 绕开方式：用 `start_new_session=True` 起一个**脱离当前进程组**的 detach 子进程。
#   它不会随服务被杀；它自己会调 `ctl.py update`（内含 停服→取数→重建→起服）。
#   子进程先把「我要开始了」写进状态文件，前端据此轮询展示进度。
UPDATE_STATE = os.path.join(ROOT, "logs", "update_state.json")
_update_lock = threading.Lock()


def _read_update_state():
    try:
        with open(UPDATE_STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_update_state(d):
    os.makedirs(os.path.dirname(UPDATE_STATE), exist_ok=True)
    tmp = UPDATE_STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, UPDATE_STATE)


def start_update(port):
    """起一个 detached 更新进程。已有更新在跑则拒绝（返回 (ok, msg)）。"""
    with _update_lock:
        cur = _read_update_state()
        if cur and cur.get("state") in ("starting", "running"):
            age = time.time() - float(cur.get("started_at") or 0)
            if age < 40 * 60:      # 40 分钟内视为仍在跑（正常 ~18min，留余量）
                return False, f"已有更新在进行中（已 {int(age // 60)} 分钟）"
        d = dict(state="starting", port=int(port), started_at=time.time(),
                 msg="正在启动更新进程…")
        _write_update_state(d)
        try:
            kw = dict(cwd=ROOT, stdin=subprocess.DEVNULL,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if os.name == "nt":
                kw["creationflags"] = (subprocess.DETACHED_PROCESS
                                       | subprocess.CREATE_NEW_PROCESS_GROUP)
            else:
                kw["start_new_session"] = True     # ★ 关键：脱离服务进程组，不被一起杀
            # ★ 用独立脚本而不是 `-c` 内联：
            #   1) 不依赖 inspect / 源码可读性；
            #   2) 逻辑可单独测试（`python scripts/update_runner.py --dry`）；
            #   3) 子进程里**不会** import server → 不会白吃 7GB 内存（见其文件头注释）。
            runner = os.path.join(ROOT, "scripts", "update_runner.py")
            subprocess.Popen([sys.executable, runner, "--port", str(int(port))], **kw)
        except Exception as e:  # noqa: BLE001
            d.update(state="failed", msg=f"无法启动更新进程：{e}")
            _write_update_state(d)
            return False, d["msg"]
        return True, "更新已开始"


# ================================================================ 每日定时播报
# 「工作日 15:30 自动更新数据 + 发飞书播报」——由**服务进程自己调度**。
#
# ⚠️⚠️ 为什么是线程 + detach 子进程，而不是直接在这里跑
# ----------------------------------------------------
# 播报链路（daily_alert.py）第一步就是 `ctl.py update`，它会**先停服**再重建面板
# （重建要独占 ~7GB）。而「停服」要停的正是**本进程** —— 天真地在本进程里跑，
# 会把自己杀掉，更新流程随进程组一起死掉。
# 所以：**调度线程只负责「到点了 → 拉起一个脱离进程组的子进程」**，
# 与界面「更新数据」按钮走**完全同一条**成熟路径（见 update_runner.py 的注释）。
#
# ⚠️ 子进程里**绝不能 import server**（会再吃 7GB → OOM）。
#    daily_alert.py 只依赖标准库 + 自己的解释器探测，天然满足。
#
# 为什么不用 macOS launchd / crontab
# ---------------------------------
# 用户明确要求「用后端定时任务」：服务本来就在跑，没必要再依赖系统级配置
# （launchd 还要手动加载、失效了没提示）。代价是**服务必须开着**才触发 ——
# 对「本地常驻选股器」来说这是合理前提。
#
# 开关与状态
# ----------
#   ASTOCK_ALERT_SCHEDULE=0   关掉（默认开）
#   ASTOCK_ALERT_TIME=15:30   改触发时刻（东八区）
#   状态文件 `logs/alert_schedule.json` —— 界面/命令行可查（含上次触发结果）
#
# ⚠️ 判断逻辑（何时该触发 / 下次何时）放在 `scripts/alert_sched.py` ——
#    **纯函数、零副作用**，可被 `tests/test_alert.py` 直接导入测试。
#    不能写在这里：本文件模块级就 `Engine()`（~7GB / 60s），
#    任何 import 都会付出那个代价，测试没法跑。
import alert_sched as SCHED  # noqa: E402

ALERT_STATE = os.path.join(ROOT, "logs", "alert_schedule.json")
_alert_thread = None


def _alert_sched_cfg() -> tuple[bool, int, int]:
    """(启用, hour, minute)。默认工作日 15:30；ASTOCK_ALERT_SCHEDULE=0 关闭。

    ⚠️ 默认 15:30 —— A股 15:00 收盘，15:30 时当日行情已定，
       更新约 18 分钟 → 约 15:48 发出播报。
    """
    en = SCHED.enabled_from_env(os.environ.get("ASTOCK_ALERT_SCHEDULE"))
    hh, mm = SCHED.parse_time(os.environ.get("ASTOCK_ALERT_TIME", "15:30"))
    return en, hh, mm


def _read_alert_state() -> dict:
    try:
        with open(ALERT_STATE, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:  # noqa: BLE001
        return {}


def _write_alert_state(d: dict) -> None:
    os.makedirs(os.path.dirname(ALERT_STATE), exist_ok=True)
    tmp = ALERT_STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, ALERT_STATE)


def _alert_loop(port: int) -> None:
    """后台调度循环：每 60 秒看一次。daemon 线程，随服务退出。"""
    enabled, hh, mm = _alert_sched_cfg()
    boot_at = SCHED.now_cst()          # 启动时刻 —— 用于宽限期判断（见 SCHED.due）
    print(f"[alert-sched] 每日播报调度"
          f"{'已启用' if enabled else '已关闭'}：工作日 {hh:02d}:{mm:02d}"
          f"（启动宽限 10 分钟内不触发）", flush=True)
    while True:
        try:
            time.sleep(60)
            st = _read_alert_state()
            now = SCHED.now_cst()
            if not SCHED.due(now, enabled, hh, mm, st.get("last_date"),
                             boot_at=boot_at):
                continue
            # ---- 触发：脱离进程组起 daily_alert.py（与「更新按钮」同一套路）
            script = os.path.join(ROOT, "scripts", "daily_alert.py")
            kw = dict(cwd=ROOT, stdin=subprocess.DEVNULL,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if os.name == "nt":
                kw["creationflags"] = (subprocess.DETACHED_PROCESS
                                       | subprocess.CREATE_NEW_PROCESS_GROUP)
            else:
                kw["start_new_session"] = True      # ★ 不被「停服」一起带走
            subprocess.Popen([sys.executable, script], **kw)
            _write_alert_state(dict(
                last_date=now.strftime("%Y-%m-%d"),
                started_at=now.strftime("%Y-%m-%d %H:%M:%S"),
                port=int(port), msg="已拉起 daily_alert.py（更新+播报）"))
        except Exception as e:  # noqa: BLE001 —— 调度线程绝不能因异常退出
            try:
                _write_alert_state(dict(last_date=(_read_alert_state() or {}).get("last_date", ""),
                                        msg=f"调度异常：{e}"))
            except Exception:  # noqa: BLE001
                pass


def start_alert_scheduler(port: int) -> None:
    """启动后台调度线程（幂等：重复调用只起一个）。"""
    global _alert_thread
    if _alert_thread is not None and _alert_thread.is_alive():
        return
    _alert_thread = threading.Thread(target=_alert_loop, args=(port,),
                                     daemon=True, name="alert-sched")
    _alert_thread.start()


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8770
    host = "127.0.0.1"
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    # ---- 每日定时播报：后台 daemon 线程（工作日 15:30 拉起 daily_alert.py）
    #   ⚠️ 必须在 serve_forever() **之前**启动，否则不会跑。
    #   ⚠️ 它是 daemon 线程 + 只读配置，起失败也不该拦住服务（函数内已兜异常）。
    start_alert_scheduler(port)
    print("=" * 72)
    print(f"  {mark('🚀', '[>>]')} 界面已启动： http://{host}:{port}/")
    print(f"     默认参数 = K3 最优 3 条件（已在报告第12节验证）")
    print("=" * 72, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  已停止")


if __name__ == "__main__":
    main()
