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
    segs = [(1417363200000, 1672416000000),   # 2014-12-01 ~ 2022-12-31
            (1672416000000, 1790000000000)]   # 2022-12-31 ~ 2026-09
    for name, code in INDICES.items():
        rows = {}
        for s, e in segs:
            r = api("/api/a-share-index/prices/historical",
                    {"thscode": code, "interval": "1d", "start": s, "end": e})
            if r.get("code") == 0:
                for it in r["data"]["item"]:
                    rows[it["date_ms"]] = it
            time.sleep(0.3)
        items = [rows[k] for k in sorted(rows)]
        json.dump(items, open(os.path.join(RAW, f"index_{name}.json"), "w"))
        print(f"{name} ({code}): {len(items)} bars")


if __name__ == "__main__":
    main()
