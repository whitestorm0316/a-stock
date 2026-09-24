#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
20_fetch_industry.py — 拉取同花顺行业指数(881xxx.TI)成分股, 建立股票->行业映射

背景:
  fuyao API 不提供股票的行业分类字段, 但提供同花顺行业指数的成分股列表。
  因此用"行业指数 -> 成分股"反向建立映射(当前快照)。

产出:
  data/raw/industry_map.json   { "000001.SZ": {"code":"881xxx.TI","name":"银行"}, ... }
  data/raw/industry_members.json { "881143.TI": {"name":"医药商业","members":[...]}, ... }

局限(必须在报告中说明):
  - 成分股是【当前快照】, 存在幸存者偏差与行业迁移误差
  - 个股所属行业按当前分类固定, 未反映历史变更
"""
import json
import os
import time
import urllib.request
import urllib.error

BASE = "https://fuyao.aicubes.cn"
KEY = os.environ.get("FUYAO_KEY", "sk-fuyao-LcCu-ioaupkOIvh4ucQ_Ab6wJhefxdQG")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, "data", "raw")


def api_get(path, params=None, retries=4, timeout=30):
    url = BASE + path
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(url, headers={"X-api-key": KEY})
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2 * (i + 1)); continue
            raise
        except Exception:
            if i == retries - 1:
                raise
            time.sleep(1.2 * (i + 1))
    raise RuntimeError("fail: " + url)


def main():
    # 1. 取全部 a-share-index 列表, 筛出 881xxx 同花顺行业指数
    r = api_get("/api/meta/tickers/list", {"asset_type": "a-share-index", "limit": 3000})
    items = r["data"]["item"]
    indices = [x for x in items if x["thscode"].startswith("881") and len(x["thscode"]) == 9]
    print(f"行业指数数量: {len(indices)}")

    # 2. 逐个拉成分股
    members = {}
    stock2ind = {}
    dup = 0
    for i, idx in enumerate(indices):
        code, name = idx["thscode"], idx["name"]
        try:
            rr = api_get("/api/a-share-index/constituents/ths-stock-list", {"thscode": code})
        except Exception as e:
            print(f"  [{i+1}/{len(indices)}] {code} {name} FAILED: {str(e)[:60]}")
            continue
        mm = []
        for it in (rr.get("data") or {}).get("item") or []:
            ts = it.get("thscode")
            if not ts:
                continue
            mm.append({"thscode": ts, "name": it.get("name")})
            if ts in stock2ind:
                dup += 1
            else:
                stock2ind[ts] = {"code": code, "name": name}
        members[code] = {"name": name, "members": mm}
        print(f"  [{i+1}/{len(indices)}] {code} {name}: {len(mm)} 只")
        time.sleep(0.12)

    print(f"\n覆盖股票数: {len(stock2ind)}  重复归属(取先出现的): {dup}")

    json.dump(stock2ind, open(os.path.join(RAW, "industry_map.json"), "w"),
              ensure_ascii=False)
    json.dump(members, open(os.path.join(RAW, "industry_members.json"), "w"),
              ensure_ascii=False)
    print("saved industry_map.json / industry_members.json")

    # 3. 与全A标的清单对齐, 看覆盖率
    tk = json.load(open(os.path.join(RAW, "tickers_ashare.json")))
    allc = {x["thscode"] for x in tk["data"]["item"]}
    covered = allc & set(stock2ind)
    print(f"全A标的 {len(allc)} 只, 有行业归属 {len(covered)} 只 "
          f"({len(covered)/max(len(allc),1)*100:.1f}%), 无归属 {len(allc - set(stock2ind))} 只")

    # 行业规模分布
    sz = sorted(((v["name"], len(v["members"])) for v in members.values()),
                key=lambda x: -x[1])
    print("\n最大的 8 个行业:", sz[:8])
    print("最小的 8 个行业:", sz[-8:])


if __name__ == "__main__":
    main()
