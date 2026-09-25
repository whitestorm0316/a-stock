# A股多因子研究项目

从原始行情到因子研究、回测、报告的完整流水线，以及一个可交互的本地选股器。

## 一、快速开始

> **Windows 用户**：数据重建好之后，双击仓库根目录的 `start.cmd` 就行，不必开终端。

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

### 启动与日常运维

统一入口 `scripts/ctl.py`（Windows / macOS / Linux 通用）。
Windows 另有根目录的双击入口：

| 想做的事 | Windows 双击 | 命令行（跨平台） |
|---|---|---|
| 启动 | `start.cmd` | `python scripts/ctl.py start` |
| 停止 | `stop.cmd` | `python scripts/ctl.py stop` |
| 重启 | — | `python scripts/ctl.py restart` |
| 查看状态 | — | `python scripts/ctl.py status` |
| **每日更新** | `update.cmd` | `python scripts/ctl.py update` |

三点须知：

- 启动要加载 3.6GB 面板，**约 80~100 秒**。加载期间端口已在听但服务未就绪，
  访问会返回 **502，这不代表坏了**；`ctl.py` 会轮询 `/api/meta` 判断真正就绪后再开浏览器。
- `update` 会**先停服务**再更新 —— 服务常驻约 7GB，与重建面板抢内存会 OOM。
  完整流程（含重建面板）约 **18 分钟**，只更新原始数据约 **10 分钟**。
- 日志在 `logs/server-<port>.log`；更新期间服务不可用。

细节见 `app/README.md`。

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
bash app/start.sh          # 默认 8770
bash app/start.sh 9000     # 自定义端口
```

首次启动加载面板并预计算，**约 60~90 秒**；之后每次改参数 0.15~13 秒。

默认参数 = V3B 报告验证的最优 3 条件：

```
距MA60 D1  +  市值最小30%  +  市场净值<MA60（熊市）  →  持有 20 日
```

左侧参数面板共 9 组，除 ①~⑦ 的选股条件外，还有：
⑥ **自定义股票群**（代码清单 / **行业两级树：门类 A~S → 细分行业** / 板块 / 交易所 / 财务）
⑧ **仓位约束**（同时持仓上限 / 每日建仓上限 / 信号超额时的选股规则，实盘可执行性）
⑨ **退市风险过滤**（财务类退市 / 连续两年亏损 / 面值退市，**只用买入日已公开信息**）

详见 [`app/README.md`](app/README.md)。

## 六、免责声明

本项目为量化研究的技术演示，**不构成任何投资建议**。历史回测不代表未来表现。
特别注意：V3B 结论中约 **2/3 的收益来自小市值 beta**，而 2015-2026 恰好是小市值占优期，
存在真实的样本外风险。
