#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fin_panel.parquet 数据字典与自检

列:
  thscode                 带交易所后缀
  report_end              报告期末（3/31, 6/30, 9/30, 12/31）
  q                       报告期编号 1..4（Q4 = 年报）
  operating_income        营业收入（累计/YTD，原币元）
  parent_holder_net_profit 归母净利润（累计/YTD，原币元）
  net_profit              净利润（累计/YTD，原币元）
  publish_date            披露日（真实披露日优先，缺失用法定截止日）
  src                     actual = 真实披露日；legal = 法定截止日兜底
  fiscal_year/fiscal_period  原始财年/财期标签

⚠️ 重要口径说明
1. operating_income 是**年初至今累计值（YTD）**，不是单季值。
   → 同比增长直接比对「去年同期同报告期」即可（如 2026Q2 vs 2025Q2）。
2. publish_date 是**动态回测的关键**：交易日 T 只允许使用 publish_date <= T 的财报。
   这样任意历史时点选股都用「当时已知」的数据，无前视偏差。
"""
from __future__ import annotations
import os
import sys
import pandas as pd
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = os.path.join(ROOT, "data", "processed", "fin_panel.parquet")

if __name__ == "__main__":
    d = pd.read_parquet(P)
    print(f"行数 {len(d):,}  股票 {d.thscode.nunique():,}")
    print(f"报告期 {d.report_end.min():%Y-%m-%d} ~ {d.report_end.max():%Y-%m-%d}")
    print(f"披露日 {d.publish_date.min():%Y-%m-%d} ~ {d.publish_date.max():%Y-%m-%d}")
    print(f"真实披露日占比 {(d.src=='actual').mean():.1%}")
    print()
    print("季度分布:")
    print(d.q.value_counts().sort_index().to_string())
    print()
    print("每股票期数分布:")
    print(d.groupby("thscode").size().describe(percentiles=[.25, .5, .75]).round(1).to_string())
    print()
    print("营收非空:", f"{d.operating_income.notna().mean():.1%}",
          " 归母净利润非空:", f"{d.parent_holder_net_profit.notna().mean():.1%}")
    print()
    print("示例（600519.SH）:")
    m = d[d.thscode == "600519.SH"].sort_values("report_end").tail(6)
    print(m[["report_end", "q", "operating_income", "parent_holder_net_profit",
             "publish_date", "src"]].to_string(index=False))
