#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
update_runner.py —— 由界面「更新数据」按钮触发的**独立更新进程**

为什么单独一个文件（而不是塞在 server.py 里）
----------------------------------------------
界面上点「更新数据」时有个结构性矛盾：

  · 重建面板（02/21 号脚本）要独占约 7GB 内存 → 必须先**停服**；
  · 而「停服」要停的正是**正在处理这个 HTTP 请求的那个服务进程**。

天真做法（在服务里 fork 线程跑更新）会被自己杀掉，流程随进程组一起死掉 ——
现象是「点了更新、服务停了、数据没更新、也不自动起来」。

正确做法是起一个**脱离服务进程组**的 detach 子进程（`start_new_session=True`），
由它去跑 `ctl.py update`。本文件就是那个子进程的入口。

⚠️ 本文件**绝不能 `import server`** —— 那会重新构造 Engine()，
   再吃掉 7GB 内存 + 80 秒，紧接着 ctl.py update 又要独占内存 → 必然 OOM。
   因此这里只依赖标准库，状态文件读写就地实现一份（格式与 server.py 一致）。

状态文件：`logs/update_state.json`
  {state: starting|running|done|failed, port, started_at, finished_at, msg}
  界面 `/api/update/status` 读它渲染进度。

用法（一般由 server 自动拉起，不手敲）：
  python scripts/update_runner.py --port 8770
  python scripts/update_runner.py --dry        # 只打印将要做什么，不真跑
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "logs", "update_state.json")
RUN_LOG = os.path.join(ROOT, "logs", "update_run.log")


def _patch(**kw) -> None:
    """原子更新状态文件（先写 .tmp 再 replace，避免半截文件）。"""
    try:
        with open(STATE, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    d.update(kw)
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)


def main() -> int:
    ap = argparse.ArgumentParser(description="界面「更新数据」按钮的后台进程")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--delay", type=float, default=3.0,
                    help="启动后先等几秒再停服（默认 3s）——让 HTTP 响应先回到浏览器，"
                         "否则页面会看到「连接被掐断」而不是「更新已开始」")
    ap.add_argument("--dry", action="store_true", help="只打印将执行什么，不真跑")
    a = ap.parse_args()

    if a.dry:
        print(f"  将执行：{sys.executable} scripts/ctl.py update "
              f"--port {a.port} --no-browser")
        print(f"  状态文件：{STATE}")
        print(f"  运行日志：{RUN_LOG}")
        return 0

    time.sleep(max(0.0, a.delay))
    _patch(state="running", started_at=time.time(),
           msg="正在更新（停服→取数→重建→起服，约 18 分钟）")

    rc = None
    try:
        os.makedirs(os.path.dirname(RUN_LOG), exist_ok=True)
        with open(RUN_LOG, "a", encoding="utf-8", errors="replace") as lf:
            lf.write(f"\n===== 界面触发更新 {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
            lf.flush()
            env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8:replace")
            r = subprocess.run(
                [sys.executable, os.path.join(ROOT, "scripts", "ctl.py"),
                 "update", "--port", str(a.port), "--no-browser"],
                cwd=ROOT, env=env, stdout=lf, stderr=subprocess.STDOUT)
            rc = r.returncode
    except Exception as e:  # noqa: BLE001
        try:
            with open(RUN_LOG, "a", encoding="utf-8", errors="replace") as lf:
                lf.write(f"\n!! 更新进程异常：{e}\n")
        except OSError:
            pass

    ok = (rc == 0)
    _patch(state=("done" if ok else "failed"), finished_at=time.time(),
           msg=("更新完成" if ok else
                f"更新失败（exit={rc}），详见 logs/update_run.log"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
