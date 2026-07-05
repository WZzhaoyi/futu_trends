"""Growth-value fundamental screener."""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from futu_fundamental_screener import (  # noqa: E402
    financial_filter,
    main,
    num,
    ratio,
    safe_inv,
    simple_filter,
)

NAME = "growth_value"
DESCRIPTION = "Growth-value fundamental screener"

MARKET_CAP_MIN = {"US": 2e9, "HK": 5e9, "A": 5e9}
PE_MIN = 0.01
PE_MAX = 35.0
PB_MAX = 5.0
ROE_MIN = 8.0
REV_GROWTH_MIN = 0.0
PROFIT_GROWTH_MIN = 0.0
DEBT_ASSET_MAX = 60.0


def build_filters(market: str, ft):
    sf = ft.StockField
    q = ft.FinancialQuarter.ANNUAL
    return [
        simple_filter(sf.MARKET_VAL, MARKET_CAP_MIN[market]),
        simple_filter(sf.PE_TTM, PE_MIN, PE_MAX),
        simple_filter(sf.PB_RATE, 0.01, PB_MAX),
        financial_filter(sf.RETURN_ON_EQUITY_RATE, ROE_MIN, quarter=q),
        financial_filter(sf.SUM_OF_BUSINESS_GROWTH, REV_GROWTH_MIN, quarter=q),
        financial_filter(sf.NET_PROFIX_GROWTH, PROFIT_GROWTH_MIN, quarter=q),
        financial_filter(sf.OPERATING_CASH_FLOW_TTM, 0, quarter=q),
        financial_filter(sf.DEBT_ASSET_RATE, max_=DEBT_ASSET_MAX, quarter=q),
    ]


def score_snapshot(candidate, snap):
    turnover = num(snap.get("turnover")) or 0
    roe = ratio(snap.get("net_profit"), snap.get("net_asset"), 100) or 0
    earnings_yield = safe_inv(snap.get("pe_ttm_ratio") or snap.get("pe_ratio"), 100) or 0
    book_discount = safe_inv(snap.get("pb_ratio")) or 0
    dividend = num(snap.get("dividend_ratio_ttm")) or 0
    liquidity = min(math.log10(turnover + 1) * 8, 80) if turnover else 0
    score = roe + earnings_yield * 1.5 + book_discount * 8 + dividend * 0.5 + liquidity * 0.2
    return {"snapshot_roe": round(roe, 4), "snapshot_score": round(score, 3)}


L2_MIN_PIOTROSKI = 4


def l2_passes(candidate) -> bool:
    """--refine L2 门槛：通用 L2 的 Piotroski 式质量分 ≥ 4。"""
    l2 = candidate.get("l2") or {}
    score = l2.get("piotroski_like_score")
    return bool(l2.get("ok") and score is not None and score >= L2_MIN_PIOTROSKI)


if __name__ == "__main__":
    main(sys.modules[__name__])
