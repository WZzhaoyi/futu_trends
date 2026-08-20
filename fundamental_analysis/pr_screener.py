"""市赚率 (PR) value screener

市赚率 PR = PE / ROE / 100，其中 ROE 取小数（15% → 0.15）。
等价于 PR = PE / ROE%，ROE% 为百分数（15% → 15）。
PR ≤ 0.5 视为「半价买入优质公司」。

L1 筛选 get_stock_filter 服务端首筛 + 返回字段的 Python 精确计算：

服务端筛选（年报口径财务字段）：
  - 市值 ≥ 100 亿（本位币）、PB > 0（净资产为正）、PE_TTM ∈ (0, 30]
  - ROE ≥ 8%、ROA_TTM ≥ 1%、权益乘数 ∈ [1, 4]（净资产 ≥ 总资产 25%）
  - 净利润 > 0、经营现金流 TTM > 0、资产负债率 ≤ 65%

Python 精确计算（get_stock_filter 无法做字段间比值）：
  - PR = PE_TTM / ROE% ≤ 0.5
  - equity_multiplier 缺省/异常（<1 不可能）时剔除
  - cash_coverage = 经营现金流 TTM / 净利润：作为报告字段提示一次性/纸面利润，
    不做硬过滤 —— 珠宝/零售等存货型企业的覆盖率天然 <1，一刀切会误杀

金融/保险 ROE 失真：L1 无行业字段，靠权益乘数 + 资产负债率过滤——
杠杆性 ROE 失真正是需求要点，银行/多数保险/地产在此被排除。

周期/一次性收益：L1 只有当期 ROE，无多年历史，
完整检测需要外部多年数据；L1 以 OCF>0 + cash_coverage 字段作提示。
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from futu_fundamental_screener import (  # noqa: E402
    _candidate_from_filter_row,
    financial_filter,
    main,
    num,
    ratio,
    row_value,
    safe_inv,
    simple_filter,
)

NAME = "pr"
DESCRIPTION = "市赚率 (PR=PE/ROE/100, PR<=0.5) value screener — L1"

# ---- L1 服务端筛选常量（年报口径） ----
# 市值下限 100 亿（各市场本位币），剔除小盘股；停牌股由 snapshot 阶段剔除
MARKET_CAP_MIN = 1e10
PE_MIN = 0.01
# PR<=0.5 ⇔ PE <= ROE%*0.5；ROE>=8% 时 PE 上限本就很低。
# 30 仅用于控制候选规模（ROE=60% 的极端高 ROE 才可能 PE=30 仍合格，会被权益乘数二次过滤）。
PE_MAX = 30.0
ROE_MIN = 8.0
ROA_MIN = 1.0
DEBT_ASSET_MAX = 65.0

# ---- PR 与假高 ROE 剔除 ----
PR_MAX = 0.5
# 权益乘数 = 总资产/净资产（futu 服务端字段，年报口径）。
# >4 ⇔ 净资产 < 总资产 25% → 高杠杆/低基数，ROE 失真；<1 为数据异常。
EQUITY_MULTIPLIER_MIN = 1.0
EQUITY_MULTIPLIER_MAX = 4.0


def build_filters(market: str, ft):
    sf = ft.StockField
    q = ft.FinancialQuarter.ANNUAL
    return [
        simple_filter(sf.MARKET_VAL, MARKET_CAP_MIN),
        simple_filter(sf.PB_RATE, 0.01),  # PB<=0（净资产为负/失真）直接剔除
        simple_filter(sf.PE_TTM, PE_MIN, PE_MAX, sort=ft.SortDir.ASCEND),
        financial_filter(sf.RETURN_ON_EQUITY_RATE, ROE_MIN, quarter=q),
        financial_filter(sf.ROA_TTM, ROA_MIN, quarter=q),
        financial_filter(sf.EQUITY_MULTIPLIER,
                         EQUITY_MULTIPLIER_MIN, EQUITY_MULTIPLIER_MAX, quarter=q),
        financial_filter(sf.NET_PROFIT, 0, quarter=q),
        financial_filter(sf.OPERATING_CASH_FLOW_TTM, 0, quarter=q),
        financial_filter(sf.DEBT_ASSET_RATE, max_=DEBT_ASSET_MAX, quarter=q),
    ]


def candidate_from_filter_row(row, market):
    """算 PR 并按 PR<=0.5 / 权益乘数剔除；不合格返回 None（runner 跳过）。"""
    cand = _candidate_from_filter_row(row, market)
    pe = num(cand.get("pe_ttm"))
    roe = num(cand.get("return_on_equity_rate"))  # 百分数，如 15.2 表示 15.2%
    if pe is None or roe is None or pe <= 0 or roe <= 0:
        return None  # 缺 PE/ROE 无法验证，丢弃
    pr = pe / roe  # PR = PE / (ROE%*100) * 100 = PE / ROE%
    cand["pr"] = round(pr, 4)
    if pr > PR_MAX:
        return None

    # 假高 ROE ①：高杠杆/低基数 —— 权益乘数 = 总资产/净资产
    equity_multiplier = num(row_value(row, "equity_multiplier"))
    if (
        equity_multiplier is None
        or equity_multiplier < EQUITY_MULTIPLIER_MIN
        or equity_multiplier > EQUITY_MULTIPLIER_MAX
    ):
        return None  # 数据缺失或异常（<1 不可能：净资产>总资产）
    cand["equity_multiplier"] = round(equity_multiplier, 4)

    # 一次性/纸面利润提示：经营现金流/净利润（不硬过滤，详见模块 docstring）
    cand["cash_coverage"] = round(ratio(cand.get("operating_cash_flow_ttm"),
                                        cand.get("net_profit")), 4)

    # 盈利增速修正 PR（仅提示，不参与过滤）
    growth = num(cand.get("net_profix_growth"))
    cand["pr_growth_adjusted"] = (
        round(pr / (1 + growth / 100), 4) if growth is not None else None
    )
    return cand


def score_snapshot(candidate, snap):
    turnover = num(snap.get("turnover")) or 0
    pr = num(candidate.get("pr"))
    earnings_yield = safe_inv(snap.get("pe_ttm_ratio") or snap.get("pe_ratio"), 100) or 0
    book_discount = safe_inv(snap.get("pb_ratio")) or 0
    dividend = num(snap.get("dividend_ratio_ttm")) or 0
    liquidity = min(math.log10(turnover + 1) * 8, 80) if turnover else 0
    pr_score = min(0.5 / pr, 4.0) if pr else 0  # PR 越小越好：0.125→4，0.5→1
    score = pr_score * 50 + earnings_yield * 1.5 + book_discount * 5 + dividend + liquidity * 0.2
    return {"snapshot_pr": round(pr, 4), "snapshot_score": round(score, 3)}


if __name__ == "__main__":
    main(sys.modules[__name__])
