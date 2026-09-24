#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server.py —— 「A股超跌反转」交互式选股器 本地服务

启动：python app/server.py            （默认 127.0.0.1:8770）
      python app/server.py 9000       （自定义端口）

接口：
  GET  /                    前端界面
  GET  /api/meta            元信息（交易日范围/行业/股票清单）
  POST /api/scan            今日选股
  POST /api/backtest        策略回测
  POST /api/tune            参数寻优（简单网格）
  GET  /api/stock?code=xxx  单股透视
  POST /api/export          导出候选股 CSV
"""
import os
import sys
import io
import csv
import json
import time
import gzip
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engine import Engine, DEFAULT_PARAMS  # noqa: E402

import numpy as np  # noqa: E402

APP = os.path.dirname(os.path.abspath(__file__))
_html_cache = {}

print("=" * 72)
print("  正在加载面板并预计算（约 60~90 秒，请稍候）...")
print("=" * 72, flush=True)
ENGINE = Engine()
print(f"  ✅ 就绪：{ENGINE.C['n']:,} 行 / {len(ENGINE.C['starts']):,} 只 / "
      f"{ENGINE.nd:,} 交易日 / 耗时 {ENGINE.build_ms/1000:.1f}s", flush=True)


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
            if p == "/api/stock":
                q = parse_qs(u.query)
                code = (q.get("code") or [""])[0]
                d = ENGINE.stock_detail(code)
                return self._json(d or {"error": "未找到该股票"}, 200 if d else 404)
            if p == "/api/defaults":
                return self._json(dict(defaults=DEFAULT_PARAMS,
                                       presets=PRESETS,
                                       optbest=ENGINE.meta()["last_date"]))
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
            if p == "/api/backtest":
                return self._backtest(b)
            if p == "/api/tune":
                return self._tune(b)
            if p == "/api/export":
                return self._export(b)
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

    def _backtest(self, b):
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
        # 净值曲线抽取（按周采样，前端足够平滑）
        step = max(1, ENGINE.nd // 1400)
        def curve(nv):
            v = nv[::step]
            return [round(float(x), 4) for x in v]
        dstr = [str(x) for x in ENGINE.day_str[::step]]
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
            nav=curve(st["nav"]), nav_gross=curve(st["nav_gross"]),
            dates=dstr,
            bench=(dict(cagr=base["cagr"], mdd=base["mdd"], sharpe=base["sharpe"],
                        nav=curve(base["nav"])) if base else None),
            hold=hold,
        )
        return self._json(out)

    def _tune(self, b):
        """参数寻优：对指定维度做小网格，返回按 Sharpe 排序的组合"""
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
            "size_max": [0, 1, 2, 3, 4, 6, 9],
            "mkt_state": ["bear", "any", "bull"],
            "mkt_hv": ["any", "high", "low"],
            "confirm": [[], ["histup3"], ["ma5"], ["volup"], ["ma5", "histup3"]],
            "ret20": [[None, None], [1, 3], [2, 6], [1, 1], [1, 5]],
        }
        sel = {k: v for k, v in AX.items() if axes.get(k, True) and k in AX}
        if not sel:
            return self._json({"error": "请至少选择一个待寻优的维度"}, 200)

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
                    else:
                        d[k] = v
                    nxt.append(d)
            combos = nxt
        # 上限保护
        MAXC = int(b.get("max_combos") or 240)
        if len(combos) > MAXC:
            combos = combos[:MAXC]

        rows = []
        nfull = ENGINE.C["n"]
        for c in combos:
            p = dict(base)
            p.update(c)
            mask, _ = ENGINE.build_mask(p, pool)
            if mask.sum() < 200:
                continue
            st = ENGINE.stats(mask, hold)
            if st is None or st["cagr"] is None or st["sharpe"] is None:
                continue
            label = []
            a, bb = p.get("px_ma60_min"), p.get("px_ma60_max")
            if a or bb:
                label.append(f"距MA60 D{a}~D{bb}")
            if p.get("ret20_min") or p.get("ret20_max"):
                label.append(f"ret20 D{p.get('ret20_min')}~D{p.get('ret20_max')}")
            ss = p.get("size_max")
            if ss is not None and ss < 9:
                label.append({0: "最小10%", 1: "最小20%", 2: "最小30%", 3: "最小40%",
                              4: "最小50%", 6: "最小70%",
                              9: "全部"}.get(int(ss), f"市值≤D{int(ss)+1}"))
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
            rows.append(dict(
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
            ))
        rows.sort(key=lambda r: (-(r["sharpe"] or -9)))
        return self._json(dict(ms=int((time.time() - t0) * 1000),
                               n_combo=len(rows), rows=rows[:120]))

    def _export(self, b):
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
                    "HV20%", "量比", "打分", "次日可买"])
        for it in res.get("items", []):
            w.writerow([res.get("asof") or res.get("date"), it["code"], it["name"],
                        it["ind"], it["ex"],
                        it["close"], it["dist60"], it["dist20"], it["ret20"],
                        it["ret60"], it["d_px60"], it["d_ret40"], it["d_ret60"],
                        it["size_grp"], it["hv20"], it["rvol"], it["score"],
                        "是" if it["can_buy"] else "否"])
        data = "\ufeff" + buf.getvalue()
        fn = f"超跌反转候选股_{res.get('asof') or res.get('date')}.csv"
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition",
                         f"attachment; filename*=UTF-8''{fn}")
        body = data.encode("utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------ 静态
    def _serve_file(self, name, ctype):
        path = os.path.join(APP, name)
        if not os.path.isfile(path):
            return self._json({"error": f"{name} not found"}, 404)
        key = (name, os.path.getmtime(path))
        if key not in _html_cache:
            with open(path, "rb") as f:
                _html_cache[key] = f.read()
        body = _html_cache[key]
        if ctype is None:
            ctype = ("text/html; charset=utf-8" if name.endswith(".html")
                     else "application/javascript; charset=utf-8" if name.endswith(".js")
                     else "text/css; charset=utf-8" if name.endswith(".css")
                     else "application/octet-stream")
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
]


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8770
    host = "127.0.0.1"
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    print("=" * 72)
    print(f"  🚀 界面已启动： http://{host}:{port}/")
    print(f"     默认参数 = K3 最优 3 条件（已在报告第12节验证）")
    print("=" * 72, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  已停止")


if __name__ == "__main__":
    main()
