#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
_console.py —— 控制台编码引导（中文 Windows / GBK 终端专用）

为什么需要它
------------
本项目大量 print 中文，启动信息里还有 emoji。中文 Windows 的 cmd.exe 默认代码页
是 936(GBK)，而 Python 会按「控制台代码页」决定 sys.stdout 的编码，于是：

    print("  ✅ 就绪：...")   →  UnicodeEncodeError: 'gbk' codec can't encode '\u2705'
    print("  正在加载面板...") →  即使不崩，输出出来也是一堆乱码

双击 start.cmd 的用户会直接看到服务启动到一半崩掉。这不是代码 bug，是终端环境差异。

本模块只解决其中一半，**另一半必须由启动器配合**，两者缺一不可：

  ① Python 侧（本模块）：强制 stdout / stderr 走 UTF-8，且 errors="replace"
     —— 保证任何字符都**不会让进程崩掉**（最坏情况显示成 ?，而不是抛 traceback）

  ② 终端侧（start.cmd / stop.cmd / update.cmd 里的 `chcp 65001`）
     —— 把控制台代码页切成 UTF-8，中文才能正常显示。只做 ① 的话，
        往 GBK 控制台写 UTF-8 字节一样是乱码，只是不崩了而已。

只在需要时动手：终端已经是 UTF-8 时不做多余改动，避免干扰 IDE / CI 的输出。

用法（放在脚本 import 区之后、第一次 print 之前）
------------------------------------------------
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    from _console import ensure_utf8
    ensure_utf8()
"""

from __future__ import annotations

import io
import os
import sys

_UTF8 = "utf-8"
_OK_NAMES = ("utf8",)  # 'UTF-8' / 'utf_8' / 'UTF8' 归一化后的形态


def _enc_of(name: str) -> str:
    """取 sys.<name> 的编码并归一化（'UTF-8' → 'utf8'）。"""
    st = getattr(sys, name, None)
    enc = getattr(st, "encoding", None) or ""
    return enc.lower().replace("-", "").replace("_", "")


# 记录「切换之前」的终端编码：用来判断 emoji 到底能不能显示
# （我们虽然强制了 UTF-8 输出，但控制台代码页仍是 GBK 时，UTF-8 字节一样会乱码）
_ORIG = {}


def _rewrap(name: str) -> bool:
    """把 sys.<name> 重新包装成 UTF-8 / errors=replace。成功返回 True。"""
    st = getattr(sys, name, None)
    if st is None:
        return False

    # 首选：reconfigure（Python 3.7+）。保留原 buffer，最安全
    try:
        st.reconfigure(encoding=_UTF8, errors="replace")
        return True
    except Exception:  # noqa: BLE001
        pass

    # 兜底：拿底层 buffer 重新包一层（st 是自定义对象 / 已被替换时）
    buf = getattr(st, "buffer", None)
    if buf is None:
        buf = getattr(getattr(sys, "__%s__" % name, None), "buffer", None)
    if buf is None:
        return False
    try:
        setattr(sys, name, io.TextIOWrapper(
            buf, encoding=_UTF8, errors="replace", line_buffering=True))
        return True
    except Exception:  # noqa: BLE001
        return False


def ensure_utf8(force: bool = False) -> dict:
    """把 stdout / stderr 切成 UTF-8 + errors="replace"。

    force=True 时即使终端已是 UTF-8 也重设（ generally 不需要）。
    返回 {名称: 处理后的编码}，方便调用方打一行诊断。
    """
    done = {}
    for name in ("stdout", "stderr"):
        enc = _enc_of(name)
        _ORIG.setdefault(name, enc)
        if enc in _OK_NAMES and not force:
            # 已是 UTF-8：只补 errors=replace，防止个别字符（如 emoji）仍把进程炸掉
            try:
                getattr(sys, name).reconfigure(errors="replace")
            except Exception:  # noqa: BLE001
                pass
            done[name] = enc
            continue
        done[name] = "%s(replace)" % _UTF8 if _rewrap(name) else (enc or "?")
    return done


def emoji_ok() -> bool:
    """原始终端是否 UTF-8 —— 决定输出里能不能用 emoji。

    注意判据是**切换前**的编码：本模块把输出强制成 UTF-8 之后，
    控制台代码页若是 GBK，写出去的 UTF-8 字节照样是乱码。
    """
    orig = _ORIG.get("stdout") or _enc_of("stdout")
    return orig in _OK_NAMES


def mark(emoji: str, ascii_alt: str) -> str:
    """能用 emoji 就返回 emoji，否则退回 ASCII 标记。"""
    return emoji if emoji_ok() else ascii_alt


def bootstrap(quiet_env: str = "ASTOCK_QUIET_CODEPAGE") -> dict:
    """ensure_utf8() + 可选的一行诊断输出。

    设置环境变量 ASTOCK_QUIET_CODEPAGE=1 可关掉那行诊断（供测试/CI 使用）。
    """
    info = ensure_utf8()
    if not os.environ.get(quiet_env):
        changed = [k for k, v in info.items() if "replace" in v]
        if changed:
            print("[console] 检测到非 UTF-8 终端（%s），已切为 UTF-8 输出；"
                  "若中文仍乱码，请在终端执行 chcp 65001"
                  % ", ".join("%s=%s" % (k, v) for k, v in info.items()))
    return info


if __name__ == "__main__":
    print("before:", {n: _enc_of(n) for n in ("stdout", "stderr")})
    print("after :", ensure_utf8())
    print("中文测试 ✅ 🚀 ⚠ —— 能正常显示即成功（看不到 emoji 属正常，不会崩就行）")
