# A股多因子研究项目

从原始行情到因子研究、回测、报告的完整流水线，以及一个可交互的本地选股器。

## 一、快速开始

> **Windows 用户**：数据重建好之后，双击仓库根目录的 `start.cmd` 就行，不必开终端。
> **macOS 用户**：已经能跑的机器直接 `bash app/start.sh`（会自动挑一个装了
> pandas 的解释器，见下）；还没有环境的先看「macOS 启动」。

```bash
# 1. 环境（需要 pandas / numpy / pyarrow）
pip install pandas numpy pyarrow

# 2. 重建面板（见下文「数据重建」）
python scripts/01_fetch_data.py        # 拉原始行情（需 API key）
python scripts/01b_fetch_gap.py        # 补齐早期日K
python scripts/01c_fetch_index.py      # 指数K线（API 限制跨度≤10年，必须分段拉）
python scripts/20_fetch_industry.py    # → industry_map.json，是 21 的硬依赖
python scripts/02_build_dataset.py     # → data/processed/panel.parquet
python scripts/21_build_v2_features.py # → data/processed/v2_panel.parquet

# 3. 启动交互式选股器（跨平台）
python scripts/ctl.py start            # http://127.0.0.1:8770/
```

### macOS 启动

```bash
cd /Users/chenqifeng/code/a-stock
bash app/start.sh                 # 默认 8770，就绪后自动开浏览器
bash app/start.sh 9000            # 换端口
bash app/start.sh --no-browser    # 不开浏览器
```

`app/start.sh` 会**自己挑一个装了 pandas/numpy/pyarrow 的解释器**。这一点很重要：

> ⚠️ **坑：`python3` 在 PATH 上 ≠ 能跑这个服务。**
> 启动逻辑（`scripts/ctl.py`）只用标准库，所以 `status` / `stop` 在任何 python3 下都正常；
> 但真正的服务进程 import numpy/pandas，而**子进程用的就是同一个解释器**。
> 一旦 PATH 上的 `python3` 没装 pandas（本机就是：托管 python3 无 pandas），
> 会出现「`status` 一切正常、只有 `start` 失败」的别扭现象，
> 且报错是子进程里一句 `ModuleNotFoundError: No module named 'numpy'`，
> **完全看不出是解释器选错了**。
>
> 现在两道防线都补上了：
> - `app/start.sh` 逐个候选**试 import**，跳过不可用的并提示一句
>   （`提示：…缺少 pandas/numpy，已改用 …`）；项目环境（3.13 + pandas 3.0）**优先于**系统 python。
> - `ctl.py start` 起服务**之前**先体检，缺包直接给三条可操作建议并以 `exit 2` 退出
>   （不再甩 traceback）。
>
> 想强制指定解释器：`PYTHON=/path/to/python3 bash app/start.sh`
> （Windows 的 `start.cmd` 同样认 `PYTHON` 环境变量）。

自查当前会用哪个解释器：

```bash
"/path/to/python3" -c "import pandas, numpy, pyarrow"    # 无输出 = 可以
python3 scripts/ctl.py status                            # 看服务与数据状态
```

### 启动与日常运维

统一入口 `scripts/ctl.py`（Windows / macOS / Linux 通用）。
Windows 另有根目录的双击入口：

| 想做的事 | Windows 双击 | macOS / Linux | 跨平台 |
|---|---|---|---|
| 启动 | `start.cmd` | `bash app/start.sh` | `python scripts/ctl.py start` |
| 停止 | `stop.cmd` | — | `python scripts/ctl.py stop` |
| 重启 | — | — | `python scripts/ctl.py restart` |
| 查看状态 | — | — | `python scripts/ctl.py status` |
| 每日更新 | `update.cmd` | `$CTL scripts/ctl.py update` | ⚠️ 见「每日更新」节 |
| **更新 + 飞书提醒** | `daily_alert.cmd` | `bash scripts/daily_alert.sh` | `python scripts/daily_alert.py` |

> `$CTL` = 任意**装了 pandas 的** python。`status` / `stop` 用裸 `python` 没问题
> （只需标准库），但 `start` / `update` 不行 —— 服务与更新链路都要 pandas。

三点须知：

- 启动要加载 3.6GB 面板，**约 80~100 秒**。加载期间端口已在听但服务未就绪，
  访问会返回 **502，这不代表坏了**；`ctl.py` 会轮询 `/api/meta` 判断真正就绪后再开浏览器。
- ⚠️ **在带 HTTP 代理的环境里 `curl` 会骗你**：若 `http_proxy` 指向一个本地代理，
  即使服务没起来，`curl http://127.0.0.1:8770/` 也可能回 **502**（代理自己的错误页），
  看起来像「服务崩了」。判断真实状态用 `curl --noproxy '*' …` 或
  `lsof -nP -iTCP:8770 -sTCP:LISTEN`。
- `update` 会**先停服务**再更新 —— 服务常驻约 7GB，与重建面板抢内存会 OOM。
  完整流程（含重建面板）约 **18 分钟**，只更新原始数据约 **10 分钟**。
- 日志在 `logs/server-<port>.log`；更新期间服务不可用。
- 启动前 `ctl.py` 会**检查解释器是否装了 pandas**，缺了直接给建议并退出
  （`exit 2`），不会只在子进程里留一句 numpy 的 traceback。

细节见 `app/README.md`。

### 每日更新

**手动更新**（想立刻拉一次当日数据时）：

```bash
bash scripts/daily_alert.sh                   # ★ 推荐：更新 + 检查信号 + 必要时发提醒
bash scripts/daily_alert.sh --skip-update     # 只检查，不更新（秒级）
```

只要更新、不要提醒的话：

```bash
~/.workbuddy-ai/binaries/python/envs/default/bin/python scripts/ctl.py update
```

> ⚠️ **别写裸 `python scripts/ctl.py update`**：`ctl.py` 自身只用标准库，
> 但更新链路是 `sys.executable` 起的子进程，依赖 pandas/pyarrow。
> 命中没装 pandas 的 `python3` 时会失败（本机就是）。
> 现在会在**停服之前**先体检并给出可操作提示（`exit 2`），服务不受影响。

`ctl.py update` 的常用变体（下面用 `$CTL` 代表一个**可用的** python，见上面的警告）：

```bash
CTL=~/.workbuddy-ai/binaries/python/envs/default/bin/python     # 或任何装了 pandas 的 python
```

| 命令 | 作用 | 耗时 |
|---|---|---|
| `$CTL scripts/ctl.py update` | 停服 → 增量取数 → 重建面板 → 起服 | ~18 min |
| `$CTL scripts/ctl.py update --no-rebuild` | 只更新原始数据（不改面板） | ~10.5 min |
| `$CTL scripts/ctl.py update --industry` | 顺带刷新行业归属（新股用，建议每周一次） | +1 min |
| `$CTL scripts/ctl.py update --since 2026-09-01` | 回补某段区间 | 视区间 |
| `$CTL scripts/ctl.py update --no-start` | 只更新，不自动起服务 | — |

> ⚠️ **`update` 会停掉服务**：服务常驻约 7GB，与重建面板（02/21）争内存会 OOM。
> 更新期间页面不可用，跑完会自己把服务拉起来（`--no-start` 除外）。
>
> ⚠️ **解释器体检在「停服」之前**：更新链路（`99_daily_update.py` →
> `02_build_dataset.py` / `21_build_v2_features.py`）全部是 `sys.executable` 起的子进程，
> 一样依赖 pandas/pyarrow。若解释器选错，旧版会**先把服务停掉、再花十几分钟取数、
> 最后才在重建面板那步炸掉** —— 比 `start` 失败更贵。现在缺包直接 `exit 2`，服务不动。

### 每日更新 + 飞书提醒（无人值守用这条）

```bash
bash scripts/daily_alert.sh                   # 更新 → 确认服务 → 满足条件就发飞书
bash scripts/daily_alert.sh --skip-update     # 跳过更新，只用现有面板检查（秒级）
bash scripts/daily_alert.sh --check           # 只自检发送通道与连通性
bash scripts/daily_alert.sh --dry-run         # 不真发，只打印消息
```

Windows 双击 `daily_alert.cmd`（等价）。macOS 上的 `scripts/daily_alert.sh`
是同一个入口的薄封装，参数完全一致。

> ⚠️ **定时请用 `bash scripts/daily_alert.sh`，别用裸 `python`**：
> 系统计划任务（cron / 计划任务）的 PATH 与登录 shell 不同，
> 命中的极可能是没装 pandas 的解释器。`.sh` 会自己挑可用的。

**macOS 上已经在跑定时**：WorkBuddy automation「A股熊市有信号提醒」，
**周一至周五 19:30**。不需要再配 cron；要改时间或提示词就到 automation 里改。

**触发条件（两条同时满足才发，否则静默退出、不发任何消息）**

| 条件 | 字段 |
|---|---|
| 最新交易日是**熊市** | `/api/scan` 的 `mkt_bull_now == False`（自建等权净值 < 均线） |
| 最新交易日**有信号** | `latest_n > 0`（默认 K3：距MA60 D1 + 市值最小30% + 熊市） |

⚠️ 注意是 `mkt_bull_now`（对应**最新交易日**）而不是 `mkt_bull`（对应被回溯的候选日）——
两者在发生回溯时会给出不同答案。**大多数交易日不会触发**（A股多数时间在 MA60 上方），
「没收到消息」通常是正常的，不是故障。

#### 配置发送通道（让其他人也能收到）

推荐用**飞书群自定义机器人 webhook** —— 只需要一条 URL，**不需要装 node/lark-cli、
不需要任何身份与权限**，把机器人拉进群，群里所有人都会收到。

1. 在目标飞书群里：**设置 → 群机器人 → 添加机器人 → 自定义机器人**，复制它的 Webhook 地址。
2. 把这条 URL 放到下面**任选一处**（优先级从高到低）：
   - 环境变量 `A_STOCK_ALERT_WEBHOOK`
   - 仓库内 `data/feishu_webhook.txt`（`data/` 已在 `.gitignore`，**密钥不会入库**）
   - 用户级 `~/.a-stock/feishu_webhook.txt`（Windows：`%USERPROFILE%\.a-stock\feishu_webhook.txt`）
3. 验证：`bash scripts/daily_alert.sh --check` 应显示「可以发送 ✅」。
4. 想真发一条测试消息：`bash scripts/daily_alert.sh --dry-run`（先看内容，不加 `--force` 不会真发）。

> 文件里允许有空行和 `#` 注释行；BOM（记事本存 UTF-8 会带）、首尾引号、
> `KEY=VALUE` 写法、行尾注释都能被自动清理，不用手工规整。

没配 webhook 时会回退到 **lark-cli bot 私信**（需要 node + `lark-cli auth status`，
且对个人 open_id 发送 —— 只适合本机自己收）。**要给一群人用，请务必用 webhook。**

#### 内置护栏（无人值守必需）

- **数据新鲜度**：最新交易日距今超过 `--max-stale`（默认 4）个自然日 → **不发**，
  防止「当日更新失败、面板还是旧的」时推一条日期很旧的提醒。
- **兜底起服**：`ctl.py update` 只在更新**成功**时才起服；失败时服务停着，
  `daily_alert.py` 第 2 步会无条件确认并自动拉起。
- **退出码**：`0` = 正常（**含「条件不满足未发送」**）；非 0 = 出错。
  任一环节失败都会以非 0 收场，便于计划任务报警。

#### 定时运行

macOS 上已由 WorkBuddy automation「A股熊市有信号提醒」托管
（**周一至周五 19:30**，rrule `FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=19;BYMINUTE=30`）。
其他平台可用系统计划任务：

```bash
# macOS / Linux crontab
30 19 * * 1-5 cd /path/to/a-stock && bash scripts/daily_alert.sh >> logs/daily_alert.log 2>&1
```

> ⚠️ 定时任务里**别写裸 `python`**（cron 的 PATH 与你登录 shell 不同，命中的解释器
> 极可能是没装 pandas 的那个）。用 `bash scripts/daily_alert.sh` —— 它会
> `find_python()` 逐个试 import 挑可用的；或直接写绝对路径：
> `30 19 * * 1-5 cd /path/to/a-stock && ~/.workbuddy-ai/binaries/python/envs/default/bin/python scripts/daily_alert.py >> logs/daily_alert.log 2>&1`

```bat
rem Windows 计划任务（taskschd.msc 新建任务 → 操作）：
rem   程序：   C:\path\to\a-stock\daily_alert.cmd
rem   起始于： C:\path\to\a-stock
rem   触发器： 每周一~周五 19:30
```

## 二、⚠️ 数据文件（不入库）

**`data/` 与 `output/*.parquet` 已被 `.gitignore` 排除，请勿提交。**

原因：Git 不是网盘，它保存**每一次提交的完整快照**。面板文件体积如下：

| 文件 | 体积 | 说明 |
|---|---|---|
| `data/processed/v2_panel.parquet` | **3.6 GB** | V2 特征面板，143 列，1053 万行（清洗后） |
| `data/processed/panel.parquet` | **2.0 GB** | 全字段基础面板 |
| `data/raw/daily_k_10y.parquet` | 173 MB | 原始日线行情 |

三个都会**超过 GitHub 单文件 100MB 硬上限**，推送会被服务器直接拒绝。
更麻烦的是：一旦提交过，即使之后 `git rm`，文件仍永久留在历史里，
`.git` 会膨胀到 5.7GB 以上，克隆者必须下载全部历史。
彻底清除需要 `git filter-repo` 重写历史 + 强推 + 所有协作者重新克隆。

### 该传 vs 不该传

| ✅ 入库（约 3.6 MB） | ❌ 不入库（约 5.9 GB） |
|---|---|
| `scripts/` — 全部研究脚本 | `data/` — 原始与处理后面板 |
| `app/` — 选股器（引擎/服务/前端） | `output/*.parquet` — 净值曲线等二进制 |
| `output/*.csv` — 研究结果表格 | `output/*.log` — 运行日志 |
| `output/*.html` — 研究报告 | `node_modules/` `__pycache__/` |
| `README.md` `.gitignore` | `.env` `*.key` 等密钥 |

## 三、数据重建

面板由 `scripts/` 全链路生成，不需要版本控制：

```
scripts/01_fetch_data.py          # 原始行情 → data/raw/
        │  （需要同花顺金融 API key，写入 .env，勿提交）
        ▼
scripts/02_build_dataset.py       # → data/processed/panel.parquet        (2.0 GB)
scripts/20_fetch_industry.py      # → data/raw/industry_map.json
        ▼
scripts/21_build_v2_features.py   # → data/processed/v2_panel.parquet     (3.6 GB)
```

`data/raw/` 是可重建的中间产物，也已忽略；若你有原始数据备份，
可以直接从 `02_build_dataset.py` 开始省去拉取步骤。

**注意**：重建耗时较长（拉取行情 + 特征计算），且依赖外部 API 可用性。
如果只是想在本地跑选股器，建议直接保留 `data/` 目录，不要删除。

## 四、研究内容

| 阶段 | 脚本 | 报告 |
|---|---|---|
| V1 MACD 量能策略回测 | `03`~`14` | `output/A股_MACD量能策略回测研究报告.html` |
| V2 MACD × Volume 因子 | `20`~`27` | `output/A股_MACD量能_V2因子研究报告.html` |
| V3 超跌缩量反转策略 | `28`~`31` | `output/A股_超跌缩量反转策略_V3研究报告.html` |
| V3B 价格超跌反转研究 | `32`~`40` | `output/A股_价格超跌反转研究_V3B研究报告.html` |

核心共享库：`scripts/v3b_lib.py`（分块滚动/位移、`oret_sig` 收益口径、向量化十分位、统计）。

## 五、交互式选股器（`app/`）

```bash
bash app/start.sh          # macOS / Linux：默认 8770，就绪后自动开浏览器
bash app/start.sh 9000     # 自定义端口
$CTL scripts/ctl.py start  # 跨平台等价写法；$CTL = 装了 pandas 的 python（见「每日更新」节）
```

首次启动加载面板并预计算，**约 60~90 秒**（实测本机 77~135s，取决于内存压力）；
之后每次改参数 0.15~13 秒。

> ⚠️ **别用裸 `python scripts/ctl.py start` 来赌解释器**。若 PATH 上的 `python3`
> 没装 pandas，服务会在子进程里以 `ModuleNotFoundError: numpy` 秒退。
> 用 `bash app/start.sh` 会自动挑可用的解释器；细节见「macOS 启动」一节。

默认参数 = V3B 报告验证的最优 3 条件：

```
距MA60 D1  +  市值最小30%  +  市场净值<MA60（熊市）  →  持有 20 日
```

左侧参数面板共 9 组，除 ①~⑦ 的选股条件外，还有：
② **市值区间**（**双滑块选闭区间**，如「10%~20%」= 只取第 2 个分位；也支持「剔除最小 20%」这类下界筛选）
⑥ **自定义股票群**（代码清单 / **行业两级树：门类 A~S → 细分行业** / 板块 / 交易所 / 财务）
⑧ **仓位约束**（同时持仓上限 / 每日建仓上限 / **建仓节奏·阶梯建仓：给出「每批买入只数」即可取代前两项** / 信号超额时的选股规则，实盘可执行性）
⑨ **退市风险过滤**（财务类退市 / 连续两年亏损 / 面值退市，**只用买入日已公开信息**）

顶部**预设方案**下拉分「内置方案（报告已验证）」与「**我的方案**」两组：
调好参数后在名称框里起个名点「保存」即可（同名即覆盖），落盘在 `data/user_presets.json`
（跟着数据目录走，换浏览器不丢）。

详见 [`app/README.md`](app/README.md)。

## 六、免责声明

本项目为量化研究的技术演示，**不构成任何投资建议**。历史回测不代表未来表现。
特别注意：V3B 结论中约 **2/3 的收益来自小市值 beta**，而 2015-2026 恰好是小市值占优期，
存在真实的样本外风险。
