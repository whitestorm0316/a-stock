#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 SVG 图标光栅化为 PNG（用本机 Chrome 的 headless 截图，无需额外依赖）。

用法
----
    # 单个 SVG → 多个尺寸
    python scripts/make_icons.py app/icons/icon.svg app/icons 32 180 192 512

    # 对照表：把目录下若干 concept-*.svg 拼成一张 review 图，人工核对用
    python scripts/make_icons.py --sheet app/icons output/_iconsheet.png

为什么用 Chrome
---------------
SVG 里用了渐变 / 圆角 / 透明度，纯 Python 侧没有可靠的渲染器（cairosvg 需要
cairo 原生库，Windows 上装起来很折腾）。Chrome 本来就要跑前端测试，直接复用。

注意事项
--------
* `--default-background-color=00000000` 才能导出透明底（Chrome 109+）。
* `--force-device-scale-factor=1` 保证输出像素 = 请求尺寸，否则会被 DPI 缩放。
* `--no-proxy-server`：本机设了 HTTP_PROXY，file:// 请求也可能被代理拦截。
"""
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
]


def find_chrome():
    for p in CHROME_CANDIDATES:
        if p and os.path.isfile(p):
            return p
    raise SystemExit("找不到 Chrome，请用 --chrome 指定，或设 CHROME 环境变量")


def _shot(chrome, html, out, w, h):
    """把一段 HTML 截成 w×h 的 PNG。"""
    fd, tmp = tempfile.mkstemp(suffix=".html")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(html)
    try:
        cmd = [
            chrome, "--headless=new", "--disable-gpu", "--no-proxy-server",
            "--hide-scrollbars", "--disable-lcd-text",
            "--force-device-scale-factor=1",
            "--default-background-color=00000000",
            f"--window-size={w},{h}",
            f"--screenshot={os.path.abspath(out)}",
            "file:///" + tmp.replace("\\", "/"),
        ]
        r = subprocess.run(cmd, capture_output=True, timeout=120)
        if not os.path.isfile(out):
            raise SystemExit("截图失败：\n" + r.stderr.decode("utf-8", "replace")[-1500:])
    finally:
        os.unlink(tmp)


def render(chrome, svg, out, size):
    """把 svg 渲染成 size×size 的 PNG。用 <img> 缩放，保证精确落位。"""
    uri = "file:///" + os.path.abspath(svg).replace("\\", "/")
    html = (
        '<!doctype html><meta charset="utf-8">'
        '<style>html,body{margin:0;padding:0;background:transparent}'
        'img{display:block;width:%dpx;height:%dpx}</style>'
        '<img src="%s">' % (size, size, uri)
    )
    _shot(chrome, html, out, size, size)


SHEET_CSS = """
html,body{margin:0;background:#fff;color:#1c1e21;
  font:13px/1.5 -apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif}
.wrap{padding:22px 26px}
h2{font-size:13px;font-weight:600;color:#5c6270;margin:0 0 14px}
table{border-collapse:collapse}
td,th{padding:12px 16px;text-align:left;vertical-align:middle;
  border-bottom:1px solid #e4e6ea}
th{font-size:11.5px;font-weight:600;color:#8d94a3;text-transform:none}
.big img{width:128px;height:128px;display:block}
.mid img{width:48px;height:48px;display:block}
.sm img{width:32px;height:32px;display:block}
.xs img{width:16px;height:16px;display:block}
.light{background:#f5f6f8;padding:8px;border-radius:8px}
.dark{background:#202124;padding:8px;border-radius:8px}
.tab{display:inline-flex;align-items:center;gap:7px;background:#f1f3f4;
  border-radius:9px 9px 0 0;padding:7px 13px;font-size:12px;max-width:200px;
  white-space:nowrap;overflow:hidden}
.tab img{width:16px;height:16px;flex:0 0 16px}
.dark-tab{background:#35363a;color:#e8eaed}
.cap{font-size:11.5px;color:#5c6270;font-weight:600}
.mono{font-family:Consolas,monospace;font-size:11px;color:#8d94a3}
"""


def sheet(chrome, srcdir, out, names):
    rows = []
    for n in names:
        p = os.path.join(srcdir, n)
        if not os.path.isfile(p):
            continue
        uri = "file:///" + os.path.abspath(p).replace("\\", "/")
        label = n.replace("concept-", "").replace(".svg", "").upper()
        rows.append(
            '<tr>'
            f'<td class="cap">{label}</td>'
            f'<td class="big"><img src="{uri}"></td>'
            f'<td><div class="light"><div class="mid"><img src="{uri}"></div></div></td>'
            f'<td><div class="light"><div class="sm"><img src="{uri}"></div></div></td>'
            f'<td><div class="dark"><div class="sm"><img src="{uri}"></div></div></td>'
            f'<td><div class="light"><div class="xs"><img src="{uri}"></div></div></td>'
            f'<td><span class="tab"><img src="{uri}">A股超跌反转 · 选股器</span></td>'
            f'<td><span class="tab dark-tab"><img src="{uri}">A股超跌反转 · 选股器</span></td>'
            '</tr>'
        )
    html = (
        '<!doctype html><meta charset="utf-8"><style>' + SHEET_CSS + '</style>'
        '<div class="wrap"><h2>图标对照表 · 128 / 48 / 32 / 16 px · 浅底与深底</h2>'
        '<table><tr><th>方案</th><th>原图</th><th>48px</th><th>32px 浅</th>'
        '<th>32px 深</th><th>16px</th><th>浅色标签页</th><th>深色标签页</th></tr>'
        + "".join(rows) + '</table></div>'
    )
    # 行高 = 128px 原图 + 上下 padding(12*2) + 底边框 1；再加表头与页边距
    _shot(chrome, html, out, 1120, 74 + 153 * len(rows) + 70)


def main(argv):
    chrome = os.environ.get("CHROME") or find_chrome()
    if argv and argv[0] == "--sheet":
        srcdir, out = argv[1], argv[2]
        names = sorted(n for n in os.listdir(srcdir)
                       if n.startswith("concept-") and n.endswith(".svg"))
        sheet(chrome, srcdir, out, names)
        print("[OK] 对照表 ->", out)
        return 0
    if len(argv) < 3:
        print(__doc__)
        return 2
    svg, outdir, sizes = argv[0], argv[1], [int(x) for x in argv[2:]]
    os.makedirs(outdir, exist_ok=True)
    base = os.path.splitext(os.path.basename(svg))[0]
    for s in sizes:
        out = os.path.join(outdir, f"{base}-{s}.png")
        render(chrome, svg, out, s)
        print(f"[OK] {s:>4}px -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
