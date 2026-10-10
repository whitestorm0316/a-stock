#!/usr/bin/env python3
"""01c_fetch_index.py — 拉取指数K线(分段, 每段<=10年)用于市场环境分层与基准"""
import os
import json
import time
import urllib.request

BASE = "https://fuyao.aicubes.cn"
KEY = os.environ.get("FUYAO_KEY", "sk-fuyao-LcCu-ioaupkOIvh4ucQ_Ab6wJhefxdQG")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, "data", "raw")

INDICES = {
    "sh000001": "000001.SH",
    "hs300": "000300.SH",
    "zz500": "000905.SH",
    "zz1000": "000852.SH",
    # 同花顺全A（市值加权口径，界面「真实指数对照」用）。
    # ⚠️ 后缀必须是 **.TI**（同花顺指数体系）。写成 `.WI` 会返回 code=1002 无数据 ——
    #    那是 Wind 的后缀，本数据源不提供 Wind 指数（踩过一次，误判成「数据源没有」）。
    # ⚠️ 历史只到 **2023-12** 起（早期段落返回空），所以**不能**做回测过滤，
    #    只做展示对照 —— 与 sh000001 等「只覆盖 2023 起」的处境相同。
    "ths_allA": "883421.TI",
}


def api(path, params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(f"{BASE}{path}?{q}", headers={"X-api-key": KEY})
    for i in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode())
        except Exception:
            time.sleep(1.5 * (i + 1))
    raise RuntimeError("fail")


def main():
    # 两段: 2014-12 ~ 2022-12 / 2022-01 ~ 今天
    #
    # ⚠️ 第二段的结束时间**必须动态取「明天」**，不要写死常量。
    #    原先写的是 1790000000000 —— 换算过来正好是 **2026-09-21**，
    #    于是从那一天起指数数据**永远停在 09-21**（取数没报错，只是窗口到点了），
    #    表现成「指数数据莫名其妙不更新」，且没有任何告警。
    end_ms = int(time.time() * 1000) + 86_400_000
    segs = [(1417363200000, 1672416000000),   # 2014-12-01 ~ 2022-12-31
            (1672416000000, end_ms)]          # 2022-12-31 ~ 明天
    for name, code in INDICES.items():
        rows = {}
        for s, e in segs:
            r = api("/api/a-share-index/prices/historical",
                    {"thscode": code, "interval": "1d", "start": s, "end": e})
            if r.get("code") == 0:
                for it in r["data"]["item"]:
                    rows[it["date_ms"]] = it
            # ⚠️ 某一段返回非 0 **不一定是错**：数据源对不同指数有不同的历史窗口，
            #    比如 883421.TI 只有 2023-12 起 → 第一段（2014-2022）必然为空。
            #    所以这里只记录、不抛错，最终以「实际拿到多少条」为准。
            time.sleep(0.3)
        items = [rows[k] for k in sorted(rows)]
        json.dump(items, open(os.path.join(RAW, f"index_{name}.json"), "w"))
        # 打印实际覆盖范围 —— 各指数历史长度不同（2023 起 vs 2015 起），一眼可见
        if items:
            first, last = items[0]["date_ms"], items[-1]["date_ms"]
            d0 = time.strftime("%Y-%m-%d", time.gmtime(first / 1000 + 8 * 3600))
            d1 = time.strftime("%Y-%m-%d", time.gmtime(last / 1000 + 8 * 3600))
            print(f"{name:12} ({code:12}): {len(items):>5} bars  {d0} ~ {d1}")
        else:
            print(f"{name:12} ({code:12}): 0 bars  ⚠️ 无数据（检查代码后缀是否正确）")


if __name__ == "__main__":
    main()
