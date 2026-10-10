#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
42_market_alert.py —— 每日更新后的「熊市 + 有信号」飞书提醒

触发条件（**两个同时满足**才发送，任一不满足就静默退出 0）：
  1. 最新交易日是**熊市**：`mkt_bull_now == False`
     （自建等权净值 < 均线，口径/窗口由左侧 ③ 组决定，默认 全A等权 + MA60）
  2. 最新交易日**有信号**：`latest_n > 0`

为什么用 `mkt_bull_now` 而不是 `mkt_bull`
----------------------------------------
`/api/scan` 回传两套市场字段，**别混**（见 app/README）：
  · `mkt_bull`     —— 对应 `asof`（**候选日**，有信号的那天）
  · `mkt_bull_now` —— 对应 `latest_date`（**最新交易日**）

「今天是不是熊市」问的是**今天**，所以必须用 `mkt_bull_now`。
熊市条件常让最新交易日 0 信号，`scan()` 会向前回溯最多 180 个交易日找
第一个有信号的日子 —— 那种情况下 `asof != latest_date`，两个字段会给出
**不同**的答案，用错就会把「今天其实牛市」误报成熊市提醒。
本脚本额外要求 `latest_n > 0`，恰好保证 `asof == latest_date`（无回溯）。

发送通道
--------
**优先飞书自定义机器人 webhook**（`data/feishu_webhook.txt`，整个 data/ 在 .gitignore 里；
也可用环境变量 `A_STOCK_ALERT_WEBHOOK`）。配了 webhook 就用它 —— 不需要 bot/user 身份，
也不受 `im:message.send_as_user` scope 限制。
没配 webhook 时回退到 **lark-cli bot 私信**（`--as bot`，收件人 `--to`）。

⚠️ 两个通道的成功契约**不同**：webhook 判 `code == 0`；lark-cli 判 `ok == true`。

用法
----
  python scripts/42_market_alert.py --dry-run        # 只打印，不发送（推荐先跑这个）
  python scripts/42_market_alert.py                  # 满足条件才发
  python scripts/42_market_alert.py --force          # 忽略条件强发一条（自测/验收用）
  python scripts/42_market_alert.py --to ou_xxx      # 指定 lark-cli 收件人（回退通道用）
  python scripts/42_market_alert.py --port 8770

退出码
------
  0  正常（含「条件不满足，未发送」—— 这不是失败）
  1  出错：服务不可达 / 发送失败

⚠️ 前提
------
  · 本地服务已在跑（`python scripts/ctl.py start`，默认 8770）
  · 发送通道二选一：
      - 自定义机器人 webhook（推荐）：写进 `data/feishu_webhook.txt`
      - lark-cli bot 私信：`npm i -g @larksuite/cli` + `lark-cli auth status` bot 身份 ready
        （bot 私信要求「用户与机器人已有会话关系」—— 至少收过一次它的消息）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

# 中文 Windows 控制台是 GBK：print emoji/中文可能崩，统一交给 _console。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from _console import bootstrap  # noqa: E402
    bootstrap()
except Exception:  # noqa: BLE001  —— _console 缺失不致命
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(ROOT, "logs")
LOG_PATH = os.path.join(LOG_DIR, "market_alert.log")

# 收件人：陈奇峰（本机 lark-cli 登录的 user open_id）。用 --to 或 env 覆盖。
DEFAULT_TO = os.environ.get("A_STOCK_ALERT_TO") or "ou_041a4381ce5e42d4f502440ef39516ee"
DEFAULT_PORT = 8770
CST = timezone(timedelta(hours=8))


# ---------------------------------------------------------------- 工具
def _find_lark() -> str | None:
    """定位 lark-cli：PATH → 托管 node bin（连接器装在全局 npm prefix 下）→ 常见安装位置。

    ⚠️ Windows 上可执行文件名是 `lark-cli.cmd`（npm 全局安装），`shutil.which`
       能识别 PATHEXT 所以第一分支通常够用；后面的候选列表覆盖
       「装在工作流托管 node 下、但不在 PATH」的情形，Windows 分支补 `.cmd`。

    ⚠️⚠️ 托管 node **升版本会删掉旧版本目录**，而 `npm i -g @larksuite/cli` 装在
       **当时那个版本**的 prefix 下 → 升级 node 会**静默带走 lark-cli**
       （2026-10-08 从 22.22.2-3 → 22.22.2-6 时实测发生）。
       本函数按目录遍历，重装一次就跟得上；但救不了「根本不存在」。
       **webhook 通道不受影响**（不依赖 node），所以只影响回退路径。
       查：`python scripts/42_market_alert.py --check`。
    """
    p = shutil.which("lark-cli")
    if p:
        return p
    names = ("lark-cli.cmd", "lark-cli.exe") if os.name == "nt" else ("lark-cli",)
    base = os.path.expanduser("~/.workbuddy-ai/binaries/node/versions")
    if os.path.isdir(base):
        for ver in sorted(os.listdir(base), reverse=True):
            for nm in names:
                cand = os.path.join(base, ver, "bin", nm)
                if os.path.exists(cand):
                    return cand
    extra = ["/opt/homebrew/bin/lark-cli", "/usr/local/bin/lark-cli"]
    if os.name == "nt":
        appdata = os.environ.get("APPDATA") or ""
        if appdata:
            extra.insert(0, os.path.join(appdata, "npm", "lark-cli.cmd"))
    for cand in extra:
        if os.path.exists(cand):
            return cand
    return None


def log(msg: str) -> None:
    stamp = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def api(port: int, path: str, payload=None, timeout=20):
    """调本地服务。payload 为 None 走 GET，否则 POST JSON。"""
    url = f"http://127.0.0.1:{port}{path}"
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _fmt_dist(v) -> str:
    return "—" if v is None else f"{v:+.2f}%"


def stale_days(latest_date: str | None) -> int | None:
    """最新交易日距今天数（自然日）。解析失败返回 None。

    无人值守下这是**必要**的护栏：若当日数据更新失败，面板还是旧的，
    条件仍可能满足 → 会发出一条日期很旧的「提醒」，看起来像今天。
    """
    if not latest_date:
        return None
    try:
        d = datetime.strptime(str(latest_date)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    return (datetime.now(CST).date() - d).days


def day_change(mk: dict | None) -> tuple[float | None, str | None]:
    """当日**大盘涨跌幅**（%），以及对比的是哪两天 → (pct, "MM-DD→MM-DD")。

    口径 = 自建等权净值的日变化（`series[-1].nav / series[-2].nav - 1`），
    与界面「市场环境」图同源。**不是**上证指数 —— 自建净值日度再平衡、
    等权，与市值加权指数会有差异（README 有说明）。

    ⚠️ 用 nav 而不是 dist：dist 是「距均线」，含了均线滞后；问「今天涨跌多少」
       要的是净值本身的日变化。两者不是一回事。
    """
    s = (mk or {}).get("series") or []
    if len(s) < 2:
        return None, None
    a, b = s[-2], s[-1]
    na, nb = a.get("nav"), b.get("nav")
    if not isinstance(na, (int, float)) or not isinstance(nb, (int, float)) or not na:
        return None, None
    pct = (nb / na - 1.0) * 100.0
    # 日期只留 MM-DD，消息里够用且短
    f = lambda x: str(x or "")[5:10] or "?"
    return pct, f"{f(a.get('date'))}→{f(b.get('date'))}"


def _fmt_pct(v: float | None) -> str:
    """涨跌幅：带正负号；A股习惯**红涨绿跌**（这里用文字标记，不靠颜色）。"""
    if v is None:
        return "—"
    return f"{v:+.2f}%"


# ---------------------------------------------------------------- 消息
def build_message(scan: dict, mk: dict | None, top: list,
                  force: bool = False) -> str:
    """拼飞书 markdown（webhook 会转成 interactive 卡片，标题写在首行 `####`）。

    三种形态，按实际状态推导（**绝不写死结论**）：
      · 熊市 + 有信号  → 「📉 熊市有信号」，列候选
      · 非熊市         → 「📈 无信号」（或「熊市但无信号」）
      · 均线未成形     → 「⚠️ 无法判定」

    ⚠️ 文案必须由 `mkt_bull_now` / `latest_n` 推导：
       `--force` 自测会在不满足条件时也发送，写死就会出现
       「净值低于 MA60（+3.40%）→ 熊市」这种自相矛盾的话。
    """
    d = scan.get("latest_date") or scan.get("asof")
    idx = scan.get("mkt_index_name") or "全A等权"
    ma = scan.get("mkt_ma") or 60
    n = int(scan.get("latest_n") or 0)
    bull = scan.get("mkt_bull_now")
    chg, chg_span = day_change(mk)

    # ⚠️ `mk["dist_now"]` 恒为**MA60**口径（market_series 默认），与 `mkt_ma` 未必一致：
    #    用户把均线窗口改成 20 时，dist_now 还是 MA60 的 —— 直接拿来用就会出现
    #    「净值低于 MA20（+0.51%）」这种**符号自相矛盾**的话。
    #    所以优先从 windows 里按窗口精确取；取不到才退回（单窗口的旧接口形态）。
    #
    # ⚠️ 只修 dist，**不动 bull**：bull 必须与主流程的判定同源（都是
    #    `scan["mkt_bull_now"]`），否则会出现「判定按 mkt_bull_now 走、文案按 windows 走」
    #    而两者不一致时自相矛盾（改这里踩过）。
    dist_now = None
    for _w in ((mk or {}).get("windows") or []):
        if _w.get("ma") == ma:
            dist_now = _w.get("dist")
            break
    if dist_now is None:
        dist_now = (mk or {}).get("dist_now", scan.get("mkt_dist"))

    # ---- 标题 + 市场状态行
    if bull is False:
        bear_txt, above = "熊市", "低于"
    elif bull is True:
        bear_txt, above = "牛市", "高于"
    else:
        bear_txt, above = None, None

    if bull is None:
        title = f"#### ⚠️ 无法判定 · {d}"
        mkt_line = f"**市场**：{idx} vs MA{ma} —— 均线尚未成形，无法判定牛熊"
    else:
        icon = "📉" if bull is False else "📈"
        title = f"#### {icon} {bear_txt} · {'有信号' if n > 0 else '无信号'} · {d}"
        mkt_line = (f"**市场**：{idx} 净值**{above}** MA{ma}"
                    f"（{_fmt_dist(dist_now)}）→ **{bear_txt}**")

    L = [title, "", mkt_line]

    # ---- 当日大盘涨跌幅（用户明确要求）
    if chg is not None:
        L.append(f"**今日大盘**：{_fmt_pct(chg)}（{idx}净值，{chg_span}）")
    else:
        L.append("**今日大盘**：—（净值序列不足，无法计算）")
    L.append("")

    # ---- 信号行
    if n > 0:
        L.append(f"**信号**：最新交易日 **{n} 只**符合默认 K3 条件")
    else:
        # 无信号时把「差多少」讲清楚 —— 这是用户要求的重点
        L.append("**信号**：最新交易日 **0 只**（不满足买入条件）")
        L.append("")
        if bull is False:
            L.append(f"> 已是熊市（净值低于 MA{ma} {_fmt_dist(dist_now)}），"
                     f"但当日**没有个股**同时满足「距MA60 最弱 10% + 市值最小 30%」。")
        elif bull is True:
            # 「还差多少」要的是**距离**，显示绝对值；带正号读起来像收益
            gap = abs(dist_now) if isinstance(dist_now, (int, float)) else None
            gap_s = "—" if gap is None else f"{gap:.2f}%"
            L.append(f"> 当前为**牛市**：净值在 MA{ma} **上方 {_fmt_dist(dist_now)}**，"
                     f"再跌 **{gap_s}** 才跌破 MA{ma} —— 按规则不建仓，"
                     f"等跌破 MA{ma} 且出现信号再买。")
        else:
            L.append(f"> 均线尚未成形（不足 {ma} 个交易日），暂不判定牛熊。")
    L.append("")

    # ---- 候选（**只在最新交易日真有信号时才列**）
    # ⚠️ 绝不能因为 `top` 非空就列：`/api/scan` 在最新交易日 0 信号时会**向前回溯**
    #    最多 180 个交易日**找第一个有信号的日子，此时 `items` 是 asof 那天的候选，
    #    与「最新交易日」不是同一天。无信号还列出来 → 用户会以为「今天有这些票」。
    if n > 0 and top:
        L.append("**候选（最超跌优先，前 8）**：")
        for it in top[:8]:
            nm = it.get("name") or ""
            code = str(it.get("code") or "").split(".")[0]
            dd = it.get("dist60")
            ind = it.get("ind") or ""
            dd_s = f"{dd:+.1f}%" if isinstance(dd, (int, float)) else "—"
            L.append(f"- {code} {nm}（{ind}）距MA60 {dd_s}")
        L.append("")

    # 换均线窗口会怎样 —— 小市值口径对窗口极敏感，值得一眼看到
    wins = (mk or {}).get("windows") or []
    if wins:
        parts = []
        for w in wins:
            st = "牛" if w.get("bull") else ("熊" if w.get("bull") is False else "—")
            parts.append(f"MA{w.get('ma')} {st}({_fmt_dist(w.get('dist'))})")
        L.append("**同口径换窗口**：" + " ｜ ".join(parts))
        L.append("")

    # 真实指数对照：自建净值与真实指数系统性分歧，是已知的口径陷阱。
    # ⚠️ 消息长度有限，只取 4 条；但**必须优先带上「同花顺全A」**——
    #    它是唯一「全市场 + 市值加权」的口径，与自建等权的差异最有说明力。
    #    按原顺序截断会把它（排在最后）丢掉，恰好丢掉最有价值的那条。
    all_idx = (mk or {}).get("indices") or []
    pref = [x for x in all_idx if "全A" in str(x.get("name") or "")]
    rest = [x for x in all_idx if "全A" not in str(x.get("name") or "")]
    idxs = (pref + rest)[:4]
    if idxs:
        bits = []
        for it in idxs:
            b = it.get("bull")
            st = "牛" if b else ("熊" if b is False else "—")
            bits.append(f"{it.get('name')} {st}({_fmt_dist(it.get('dist'))})")
        L.append("**真实指数对照**：" + " ｜ ".join(bits))
        L.append("")

    L.append("> 自建等权净值与交易所指数**会系统性分歧**（等权 + 日度再平衡上偏），")
    L.append("> 「熊市」以自建口径为准。详见选股器「今日选股」页市场环境表。")
    if force:
        L.append("> ⚠️ 本条为 `--force` 强制发送（忽略触发条件），用于自测。")
    return "\n".join(L)


# ---------------------------------------------------------------- 飞书
WEBHOOK_FILE = os.path.join(ROOT, "data", "feishu_webhook.txt")
# 用户级路径：不在仓库里，适合「只想收提醒、不想碰仓库」的人。
# 优先级低于仓库内文件（仓库里配了就以仓库为准），但高于 lark-cli 回退。
WEBHOOK_USER_FILE = os.path.join(os.path.expanduser("~"), ".a-stock", "feishu_webhook.txt")


def _clean_webhook(v: str | None) -> str | None:
    """把一行候选文本清理成干净的 URL，清不出来就返回 None。

    ⚠️ 三个 Windows 上极易踩的坑，都必须容忍（否则「配了却不生效」且毫无提示）：
      1. **记事本 UTF-8 会写 BOM**（\\ufeff）→ 按 utf-8 读会让 URL 首字符变成
         `\\ufeffhttps://…`，正则校验**通不过**，表现为「发了但静默不生效」。
         读取侧统一用 `utf-8-sig`，这里再兜底 strip 一次。
      2. **用户习惯给值加引号**（`"https://…"` 或 `'https://…'`）→ 引号进 URL → 校验失败。
      3. **复制粘贴带首尾空白 / 空格**，或把整行当成 key=value 写。
    """
    if not v:
        return None
    v = v.strip().lstrip("\ufeff").strip()
    # 允许写成 KEY=VALUE 形式
    if "=" in v and not v.lower().startswith("http"):
        v = v.split("=", 1)[1].strip()
    # 去掉成对的包裹引号（可能套了两层）
    for _ in range(2):
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1].strip()
        else:
            break
    # 抽第一段 http(s) 链接（容忍行内注释 / 多余文字）
    m = re.search(r"https?://[^\s\"'<>]+", v)
    return m.group(0) if m else None


def _read_first_line(path: str) -> str | None:
    """读文件里第一行非注释、非空的内容。

    ⚠️ 用 `utf-8-sig`：Windows 记事本保存的 UTF-8 带 BOM，用 `utf-8` 读会在
       首行开头留下 \\ufeff（见 _clean_webhook 的注释）。
    """
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    except OSError:
        pass
    return None


def _find_webhook() -> str | None:
    """自定义机器人 webhook，按优先级：

      1. 环境变量 `A_STOCK_ALERT_WEBHOOK`（最适合容器 / CI / 计划任务）
      2. 仓库内 `data/feishu_webhook.txt`（data/ 整个在 .gitignore → 密钥不入库）
      3. 用户级 `~/.a-stock/feishu_webhook.txt`（只想收提醒、不碰仓库的人用）

    都没配 → 返回 None，发送会回退到 lark-cli bot 私信。
    """
    v = _clean_webhook(os.environ.get("A_STOCK_ALERT_WEBHOOK"))
    if v:
        return v
    for path in (WEBHOOK_FILE, WEBHOOK_USER_FILE):
        v = _clean_webhook(_read_first_line(path))
        if v:
            return v
    return None


def send_webhook(md: str, url: str, dry: bool) -> tuple[bool, str]:
    """发自定义机器人 webhook。

    ⚠️ 用 **interactive 卡片**，不要用 post：
    自定义机器人的 `post` 段只认 text / a / at / img，塞 `md` 会返回
    `{"code":10002,"msg":"not support md tag"}`（踩过）。
    卡片的 `div`+`lark_md` 才是支持 markdown 的那条路。
    成功契约：HTTP 200 且 body `code == 0`（与 lark-cli 的 `ok == true` 不同）。
    """
    if not re.match(r"^https://open\.(feishu\.cn|larksuite\.com)/open-apis/bot/v2/hook/[\w-]+$",
                    url or ""):
        return False, f"webhook URL 形态不对：{url!r}"

    # 首行 `#### 标题` → 卡片 header，其余 → lark_md 正文
    lines = md.splitlines()
    title = "A股 · 熊市有信号提醒"
    body_lines = lines
    if lines and lines[0].lstrip().startswith("#"):
        title = lines[0].lstrip("# ").strip() or title
        body_lines = lines[1:]
    body = "\n".join(body_lines).strip()

    # 卡片 header 颜色跟着语义走（红=熊市/提醒，绿=牛市，灰=未成形）
    tmpl = "red" if "熊市" in title else ("green" if "牛市" in title else "grey")

    if dry:
        return True, f"[dry-run] 将 POST 卡片到 {url[:60]}…（{len(body)} 字符）"

    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"template": tmpl,
                       "title": {"tag": "plain_text", "content": title}},
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": body}}],
        },
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            out = r.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        return False, f"webhook 请求失败：{e}"
    try:
        j = json.loads(out)
    except ValueError:
        return False, f"webhook 返回非 JSON：{out[:300]}"
    if j.get("code") == 0 or j.get("StatusCode") == 0:
        return True, "webhook OK"
    return False, f"webhook 返回错误：{out[:400]}"


def send_feishu(md: str, to: str, idem: str, dry: bool) -> tuple[bool, str]:
    """发送提醒。**优先 webhook**（配了就用），否则回退 lark-cli bot 私信。"""
    hook = _find_webhook()
    if hook:
        return send_webhook(md, hook, dry)

    lark = _find_lark()
    if not lark:
        return False, ("既没有 webhook（data/feishu_webhook.txt 或 "
                       "A_STOCK_ALERT_WEBHOOK），也找不到 lark-cli。")
    cmd = [lark, "im", "+messages-send", "--user-id", to,
           "--markdown", md, "--as", "bot"]
    if idem:
        cmd += ["--idempotency-key", idem[:50]]
    if dry:
        cmd += ["--dry-run"]
    try:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, timeout=90)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"调用 lark-cli 失败：{e}"
    out = (r.stdout or b"").decode("utf-8", "replace")
    err = (r.stderr or b"").decode("utf-8", "replace")
    if dry:
        return (r.returncode == 0), (out or err).strip()
    # lark-cli 成功用 ok==true 判断（不要用 code==0，见 lark-shared 输出契约）
    try:
        j = json.loads(out)
        if j.get("ok") is True:
            return True, json.dumps(j.get("data") or {}, ensure_ascii=False)[:300]
    except ValueError:
        pass
    return False, (err or out).strip()[:600]


# ---------------------------------------------------------------- 自检
def do_check(a) -> int:
    """配置自检：不取信号、不发送，只回答「明天 19:30 到底能不能发出提醒」。

    典型的卡点都在这几步上，所以逐条打印结论而不是让人猜：
      · 有没有配置发送通道？配的是哪个（env / 仓库文件 / 用户目录）？
      · URL 形态对不对（BOM、引号、粘贴空格都会让它失效）？
      · 本地服务能不能连上？
    """
    print("=" * 62)
    print("  发送通道自检")
    print("=" * 62)

    src = None
    hook = None
    env_v = _clean_webhook(os.environ.get("A_STOCK_ALERT_WEBHOOK"))
    if env_v:
        hook, src = env_v, "环境变量 A_STOCK_ALERT_WEBHOOK"
    else:
        for path, name in ((WEBHOOK_FILE, "仓库文件 data/feishu_webhook.txt"),
                           (WEBHOOK_USER_FILE, "用户文件 ~/.a-stock/feishu_webhook.txt")):
            v = _clean_webhook(_read_first_line(path))
            if v:
                hook, src = v, name
                break

    rc = 0
    if hook:
        ok, why = send_webhook("#### 自检\n\n发送通道自检（--check）", hook, dry=True)
        masked = re.sub(r"(hook/)[\w-]+", r"\1****", hook)
        print(f"  通道       webhook（自定义机器人）")
        print(f"  来源       {src}")
        print(f"  地址       {masked}")
        print(f"  URL 形态   {'通过' if ok else '不合法'}")
        if not ok:
            print(f"  !! {why}")
            print("     常见原因：复制时漏了字符 / 多了引号或空格 / 用了 lark-cli 的 app 凭证")
            rc = 1
    else:
        lark = _find_lark()
        print("  通道       未配置 webhook → 回退 lark-cli bot 私信")
        print(f"  lark-cli   {lark or '未找到'}")
        print()
        print("  建议：给「其他人也能收到」请改用群机器人 webhook —— 一条 URL 即可，")
        print("        无需装 node/lark-cli、无需任何身份与权限。配置方法见 README。")
        print("        配置位置（任选其一）：")
        print(f"          · 环境变量 A_STOCK_ALERT_WEBHOOK")
        print(f"          · {WEBHOOK_FILE}")
        print(f"          · {WEBHOOK_USER_FILE}")
        if not lark:
            rc = 1

    print()
    print("-" * 62)
    print("  服务连通性")
    print("-" * 62)
    try:
        meta = api(a.port, "/api/meta", timeout=6)
        print(f"  端口 {a.port}   已就绪")
        print(f"  最近交易日     {meta.get('last_date')}")
        print(f"  标的 / 交易日  {meta.get('n_stocks')} / {meta.get('n_days')}")
    except Exception as e:  # noqa: BLE001
        print(f"  端口 {a.port}   连不上（{e}）")
        print(f"     → 先起服务：python scripts/ctl.py start")
        print(f"       （无人值守下 daily_alert.py 会自动兜底拉起，此处只是手动自检）")

    print()
    print("=" * 62)
    print(f"  自检结论：{'可以发送 ✅' if rc == 0 else '有问题，见上面 !! 行 ❌'}")
    print("  想真发一条测试消息（忽略牛熊与信号条件）：")
    print("      python scripts/42_market_alert.py --force")
    print("=" * 62)
    return rc


# ---------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser(description="熊市+有信号 的飞书提醒")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--to", default=DEFAULT_TO, help="收件人 open_id（默认本人）")
    ap.add_argument("--dry-run", action="store_true", help="只打印消息，不发送")
    ap.add_argument("--force", action="store_true", help="忽略触发条件，强制发送")
    ap.add_argument("--max-stale", type=int, default=4,
                    help="最新交易日最多可陈旧几个自然日（默认 4：覆盖周末+1 个假日）；"
                         "超过则判定「当日数据没更新成功」并跳过")
    ap.add_argument("--params", default=None,
                    help="覆盖选股参数（JSON 字符串）。默认用引擎默认 K3")
    ap.add_argument("--check", action="store_true",
                    help="只自检「发送通道配置 + 服务连通性」，不取信号也不发送")
    ap.add_argument("--always", dest="always", action="store_true", default=True,
                    help="【默认】每日必发：无信号也发一条（含距均线差距 + 当日大盘涨跌幅）")
    ap.add_argument("--only-signal", dest="always", action="store_false",
                    help="只在「熊市 + 有信号」时发送，其余静默（旧行为）")
    a = ap.parse_args()

    if a.check:
        return do_check(a)

    params = {}
    if a.params:
        try:
            params = json.loads(a.params)
        except ValueError as e:
            log(f"!! --params 不是合法 JSON：{e}")
            return 1

    # ---- 取数
    try:
        scan = api(a.port, "/api/scan", {"params": params, "limit": 10})
    except urllib.error.URLError as e:
        log(f"!! 连接本地服务失败（{a.port}）：{e}。服务起了吗？"
            f"（python scripts/ctl.py start）")
        return 1
    except Exception as e:  # noqa: BLE001
        log(f"!! /api/scan 失败：{e}")
        return 1

    if scan.get("error"):
        log(f"!! /api/scan 返回错误：{scan['error']}")
        return 1

    try:
        mk = api(a.port, "/api/market?days=180")
    except Exception:  # noqa: BLE001  —— 市场序列只是锦上添花，拿不到不阻断
        mk = None

    d = scan.get("latest_date")
    bear = scan.get("mkt_bull_now") is False
    n = int(scan.get("latest_n") or 0)
    ma = scan.get("mkt_ma") or 60
    _chg, _span = day_change(mk)

    log(f"最新交易日 {d}｜熊市={bear}（mkt_bull_now={scan.get('mkt_bull_now')}）"
        f"｜信号数 latest_n={n}"
        f"｜口径 {scan.get('mkt_index_name')} MA{scan.get('mkt_ma')}"
        f"｜当日大盘 {_fmt_pct(_chg)}"
        f"｜asof={scan.get('asof')} fallback={scan.get('fallback_days')}")

    # ---- 数据新鲜度护栏：数据没更新就别发「今天的」提醒
    # 阈值按**自然日**算，默认 4 天：刚好覆盖「周末 + 一个法定假日」，
    # 又能在更新连续失败时拦住旧数据。（交易日历不在本地，用自然日是刻意的近似；
    #  提醒正文里始终带日期，所以即便阈值放行，旧日期也一眼可见。）
    # ⚠️ 改为「每天必发」后，这条护栏更重要 —— 否则更新失败时会连发日期很旧的
    #    「今日大盘」，看起来像今天的数据。
    age = stale_days(d)
    if age is not None and age > a.max_stale and not a.force:
        log(f"!! 数据陈旧：最新交易日 {d} 距今 {age} 天（阈值 {a.max_stale}）。"
            f"当日数据可能没更新成功 → 不发提醒。"
            f"（先跑 python scripts/ctl.py update 再重试）")
        return 0

    fire = (bear and n > 0)
    if not fire and not a.always and not a.force:
        why = []
        if not bear:
            why.append("最新交易日不是熊市")
        if n <= 0:
            why.append("最新交易日无信号")
        if age is not None and age > 0:
            why.append(f"数据距今 {age} 天")
        log("条件不满足（" + "、".join(why) + "），不发提醒（--only-signal）。")
        return 0

    if fire:
        log(f"命中：熊市 + 有信号（{n} 只）→ 发送")
    else:
        reasons = []
        if not bear:
            _dn = (mk or {}).get("dist_now", scan.get("mkt_dist"))
            reasons.append(f"非熊市（净值距MA{ma} {_fmt_dist(_dn)}）")
        if n <= 0:
            reasons.append("无信号")
        log(f"未命中（{'、'.join(reasons) or '未知'}）→ "
            f"{'每天必发，仍发送' if a.always else '静默'}")

    md = build_message(scan, mk, scan.get("items") or [], force=a.force)
    log("---- 消息预览 ----")
    for line in md.splitlines():
        print("   " + line, flush=True)
    log("------------------")

    idem = f"a-stock-bear-signal-{d}"
    chan = "webhook" if _find_webhook() else "lark-cli(bot)"
    ok, info = send_feishu(md, a.to, idem, a.dry_run)
    if ok:
        log(f"{'[dry-run] 未真正发送' if a.dry_run else '已发送'}（{chan}）｜{info}")
        return 0
    log(f"!! 发送失败（{chan}）：{info}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
