"""市赚率 (PR) value screener

市赚率 PR = PE / ROE / 100，其中 ROE 取小数（15% → 0.15）。
等价于 PR = PE / ROE%，ROE% 为百分数（15% → 15）。
PR ≤ 0.5 视为「半价买入优质公司」。

L1 筛选 get_stock_filter 服务端首筛 + 返回字段的 Python 精确计算：

服务端筛选（年报口径财务字段）：
  - 市值 ≥ 100 亿（本位币）
  - 近 20 日日均成交额 ≥ 美股 1 亿美元 / 港股 3500 万港币 / A 股 1 亿人民币
  - PB > 0、PE_TTM > 0
  - ROE ≥ 8%、ROA_TTM ≥ 1%、权益乘数 ∈ [1, 4]（净资产 ≥ 总资产 25%）
  - 净利润 > 0、经营现金流 TTM > 0、资产负债率 ≤ 65%

Python 精确计算（get_stock_filter 无法做字段间比值）：
  - PR = PE_TTM / ROE% ≤ 0.5
  - equity_multiplier 缺省/异常（<1 不可能）时剔除
  - cash_coverage = 经营现金流 TTM / 净利润：作为报告字段提示一次性/纸面利润，
    不做硬过滤 —— 珠宝/零售等存货型企业的覆盖率天然 <1，一刀切会误杀

L2 精算（--refine，F10 利润表 + 资产负债表）：近 3 期年报 ROE 都 ≥ 8%，取不到即剔除。
它取代原先的「PE_TTM ≤ 11」—— 绝对倍数上限会连带砍掉高 ROE 的好公司，而多年 ROE
下限砍掉的才是周期顶点/一次性利润（当期 ROE 高、历史年份亏损）。

金融/保险 ROE 失真：L1 无行业字段，靠权益乘数 + 资产负债率过滤——
杠杆性 ROE 失真正是需求要点，银行/多数保险/地产在此被排除。
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from futu_fundamental_screener import (  # noqa: E402
    FINANCIAL_ANNUAL_TYPE,
    POSITIVE_MIN,
    _candidate_from_filter_row,
    daily_average_filter,
    financial_filter,
    main,
    num,
    ratio,
    row_value,
    safe_inv,
    simple_filter,
)

NAME = "pr"
DESCRIPTION = "市赚率 (PR=PE/ROE/100, PR<=0.5) value screener"

# ---- L1 服务端筛选常量（年报口径） ----
# 市值下限 100 亿（各市场本位币），剔除小盘股；停牌股由 snapshot 阶段剔除
MARKET_CAP_MIN = 1e10
# 流动性：近 20 日**日均**成交额（本位币），由 daily_average_filter 换算成接口的
# 累计口径后下发 —— 这里只写日均值，别在别处再乘一次。
TURNOVER_WINDOW_DAYS = 20
TURNOVER_DAILY_MIN = {"US": 100e6, "HK": 35e6, "A": 100e6}
PE_MIN = POSITIVE_MIN
ROE_MIN = 8.0
ROA_MIN = 1.0
DEBT_ASSET_MAX = 65.0
# 净利润 / 经营现金流的「为正」下限见 POSITIVE_MIN（runner 上有量化说明，勿改回 1e-12）。

# ---- PR 与假高 ROE 剔除 ----
PR_MAX = 0.5
# 权益乘数 = 总资产/净资产（futu 服务端字段，年报口径）。
# >4 ⇔ 净资产 < 总资产 25% → 高杠杆/低基数，ROE 失真；<1 为数据异常。
EQUITY_MULTIPLIER_MIN = 1.0
EQUITY_MULTIPLIER_MAX = 4.0

# ---- L2：多年 ROE 稳定性（--refine，F10 利润表 + 资产负债表）----
# 取代原先的 PE ≤ 11 绝对倍数上限：近 ROE_ANNUAL_PERIODS 期年报 ROE 每一年都
# ≥ ROE_ANNUAL_MIN，取不到年报数据一律剔除。
FUTU_L2_STATEMENT_TYPES = ("income", "balance")
ROE_ANNUAL_MIN = 8.0
ROE_ANNUAL_PERIODS = 3


def build_filters(market: str, ft):
    sf = ft.StockField
    q = ft.FinancialQuarter.ANNUAL
    return [
        simple_filter(sf.MARKET_VAL, MARKET_CAP_MIN),
        daily_average_filter(
            sf.TURNOVER, TURNOVER_DAILY_MIN[market], days=TURNOVER_WINDOW_DAYS,
        ),
        simple_filter(sf.PB_RATE, POSITIVE_MIN),  # PB<=0（净资产为负/失真）直接剔除
        simple_filter(sf.PE_TTM, PE_MIN, sort=ft.SortDir.ASCEND),
        financial_filter(sf.RETURN_ON_EQUITY_RATE, ROE_MIN, quarter=q),
        financial_filter(sf.ROA_TTM, ROA_MIN, quarter=q),
        financial_filter(sf.EQUITY_MULTIPLIER,
                         EQUITY_MULTIPLIER_MIN, EQUITY_MULTIPLIER_MAX, quarter=q),
        financial_filter(sf.NET_PROFIT, POSITIVE_MIN, quarter=q),
        financial_filter(sf.OPERATING_CASH_FLOW_TTM, POSITIVE_MIN, quarter=q),
        financial_filter(sf.DEBT_ASSET_RATE, max_=DEBT_ASSET_MAX, quarter=q),
        # 仅请求返回字段，不参与服务端筛选。
        financial_filter(sf.NET_PROFIX_GROWTH, quarter=q, is_no_filter=True),
    ]


def candidate_from_filter_row(row, market):
    """算 PR 并按 PR<=0.5 / 权益乘数剔除；不合格返回 None（runner 跳过）。"""
    cand = _candidate_from_filter_row(row, market)
    # PR 由 derive_pr 统一算出；这里只做判据，用展示值保证「筛的」就是「显示的」。
    pr = num(cand.get("pr"))
    if pr is None:
        return None  # 缺 PE/ROE 或非正，无法验证 PR，丢弃
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
    cash_coverage = ratio(cand.get("operating_cash_flow_ttm"), cand.get("net_profit"))
    cand["cash_coverage"] = (
        round(cash_coverage, 4) if cash_coverage is not None else None
    )

    # 盈利增速修正 PR（仅提示，不参与过滤）
    growth = num(cand.get("net_profix_growth"))
    cand["pr_growth_adjusted"] = (
        round(pr / (1 + growth / 100), 4)
        if growth is not None and growth > -100
        else None
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


# ---- L2 精算 ----

def refine_futu(candidate, financials):
    """算近几期年报 ROE：净利润 ÷ 期末净资产，与 L1 的 ROE 字段同源。"""
    annual = financials.aligned(
        "income", "balance", financial_type=FINANCIAL_ANNUAL_TYPE,
    )
    if not annual:
        return {"ok": False, "source": "futu",
                "note": "no common annual period across income and balance"}
    roes, periods = [], []
    for income_report, balance_report in annual:
        fields = financials.fields_for(income_report, balance_report)
        value = ratio(income_report.value(fields.net_income),
                      balance_report.value(fields.equity))
        if value is None:
            continue
        roes.append(round(value * 100, 4))
        periods.append(income_report.period_text)
    if not roes:
        return {"ok": False, "source": "futu",
                "note": "annual reports carry no usable net_income/equity"}
    recent = roes[:ROE_ANNUAL_PERIODS]
    return {
        "ok": True,
        "source": "futu",
        "report_period": periods[0],
        "financial_currency": annual[0][0].currency_code,
        "roe_periods": len(roes),
        "annual_roe_pct": roes[:6],
        "roe_recent_min_pct": min(recent),
        "roe_recent_avg_pct": round(sum(recent) / len(recent), 4),
    }


def l2_reject_reason(candidate) -> str | None:
    """L2 被拒的第一条原因；None 表示通过。仅用于计数与归因。"""
    l2 = candidate.get("l2") or {}
    if not l2.get("ok"):
        return "l2_unavailable"
    if (l2.get("roe_periods") or 0) < ROE_ANNUAL_PERIODS:
        return "roe_history_short"
    if l2["roe_recent_min_pct"] < ROE_ANNUAL_MIN:
        return "roe_below_floor"
    return None


def l2_passes(candidate) -> bool:
    """--refine L2 门槛：近 3 期年报 ROE 都 ≥ 8%，取不到即剔除。"""
    return l2_reject_reason(candidate) is None


if __name__ == "__main__":
    main(sys.modules[__name__])
