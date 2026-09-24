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
from urllib.parse import urlparse, parse_qs, quote

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engine import Engine, DEFAULT_PARAMS, PICK_RULES  # noqa: E402

import numpy as np  # noqa: E402

APP = os.path.dirname(os.path.abspath(__file__))
_html_cache = {}

print("=" * 72)
print("  正在加载面板并预计算（约 60~90 秒，请稍候）...")
print("=" * 72, flush=True)
ENGINE = Engine()
print(f"  ✅ 就绪：{ENGINE.C['n']:,} 行 / {len(ENGINE.C['starts']):,} 只 / "
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
                return self._json(dict(
                    defaults=DEFAULT_PARAMS,
                    presets=PRESETS,
                    # 容量约束：选股规则清单（供前端下拉）
                    pick_rules=[dict(id=k, name=v["name"], desc=v["desc"],
                                     default=(k == DEFAULT_PARAMS.get("pick")))
                                for k, v in PICK_RULES.items()
                                if not k.startswith("__")],
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
            if p == "/api/trades":
                return self._trades(b)
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
        # ---- 容量约束回测（实盘可执行性）：max_pos>0 时启用
        mp = int(p.get("max_pos") or 0)
        if mp > 0:
            try:
                cap = ENGINE.capacity(
                    mask, hold=hold, max_pos=mp,
                    max_new=int(p.get("max_new") or 3),
                    pick=str(p.get("pick") or "deep"),
                    n_sim=int(b.get("n_sim", 200)),
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
            if mp > 0:
                tplan = ENGINE.capacity_plan(
                    mask, hold=hold, max_pos=mp,
                    max_new=int(p.get("max_new") or 3),
                    pick=str(p.get("pick") or "deep"))
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
        # ---- 容量约束（与 /api/backtest 同口径）：max_pos>0 时只列实际建仓
        #      ⚠️ 这里用轻量版 capacity_plan()，不跑 200 次随机模拟（明细不需要分布）
        mp = int(p.get("max_pos") or 0)
        plan = None
        if mp > 0:
            try:
                plan = ENGINE.capacity_plan(
                    mask, hold=hold, max_pos=mp,
                    max_new=int(p.get("max_new") or 3),
                    pick=str(p.get("pick") or "deep"))
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
                      "fin_rev", "fin_rev_yoy", "fin_np", "fin_np_yoy"}
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
        # ⚠️ 默认必须是 False：前端只发「已勾选」的键（axes={key:true}），
        #    若用 axes.get(k, True)，未勾选的维度会被当成 True 一起寻优 →
        #    用户取消勾选完全无效（实测：只勾 mkt_state 仍跑了 240 个组合）。
        sel = {k: v for k, v in AX.items() if axes.get(k, False) and k in AX}
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
        path = os.path.join(APP, name)
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
