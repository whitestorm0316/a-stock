#!/usr/bin/env python3
"""并行分片下载器 — 用于 S3 预签名链接"""
import os
import sys
import json
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

CHUNK = 2 * 1024 * 1024      # 2MB per request
CONCURRENCY = 16


def get_url():
    key = os.environ.get("FUYAO_KEY", "sk-fuyao-LcCu-ioaupkOIvh4ucQ_Ab6wJhefxdQG")
    req = urllib.request.Request(
        "https://fuyao.aicubes.cn/api/dump/market-dumps/daily-k/download-url",
        headers={"X-api-key": key})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())["data"]["presigned_url"]


def head_size(url):
    req = urllib.request.Request(url, method="GET",
                                headers={"Range": "bytes=0-0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        cr = r.headers.get("Content-Range", "")
        if "/" in cr:
            return int(cr.split("/")[1])
    raise RuntimeError("no content-range")


def fetch(url, start, end, retries=6):
    hdr = {"Range": f"bytes={start}-{end}", "User-Agent": "Mozilla/5.0"}
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=hdr)
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except Exception as e:
            if i == retries - 1:
                raise
            time.sleep(1.0 * (i + 1))
    return b""


def main():
    dest = sys.argv[1] if len(sys.argv) > 1 else "data/raw/daily_k_10y.parquet"
    os.makedirs(os.path.dirname(dest), exist_ok=True)

    url = get_url()
    total = head_size(url)
    print(f"total size: {total/1e6:.1f} MB")

    if os.path.exists(dest) and os.path.getsize(dest) == total:
        print("already complete")
        return

    ranges = [(s, min(s + CHUNK - 1, total - 1)) for s in range(0, total, CHUNK)]
    print(f"chunks: {len(ranges)}  concurrency: {CONCURRENCY}")

    buf = {}
    t0 = time.time()
    done = 0
    with open(dest, "wb") as f:
        f.truncate(total)

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futs = {ex.submit(fetch, url, s, e): (s, e) for s, e in ranges}
        for fut in as_completed(futs):
            s, e = futs[fut]
            data = fut.result()
            buf[s] = data
            done += 1
            if done % 20 == 0 or done == len(ranges):
                el = time.time() - t0
                got = sum(len(v) for v in buf.values())
                print(f"  {done}/{len(ranges)} chunks  {got/1e6:.1f}MB  "
                      f"{el:.0f}s  {got/1e6/max(el,0.1):.1f} MB/s", flush=True)
            # 及时写盘, 避免内存膨胀
            if len(buf) >= 40:
                with open(dest, "r+b") as f:
                    for k in sorted(buf):
                        f.seek(k)
                        f.write(buf[k])
                buf.clear()

    if buf:
        with open(dest, "r+b") as f:
            for k in sorted(buf):
                f.seek(k)
                f.write(buf[k])

    sz = os.path.getsize(dest)
    print(f"done: {dest}  {sz/1e6:.1f} MB in {time.time()-t0:.0f}s")
    assert sz == total, f"size mismatch {sz} != {total}"


if __name__ == "__main__":
    main()
