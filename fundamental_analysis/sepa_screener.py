#  Futu Trends
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.

"""SEPA trend-template screener."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from futu_fundamental_screener import (  # noqa: E402
    custom_indicator_filter,
    financial_filter,
    main,
    num,
    simple_filter,
)

NAME = "sepa"
DESCRIPTION = "SEPA trend-template screener"

EMA_FAST = 50
EMA_MID = 150
EMA_SLOW = 200
LOW52_MIN = 30.0
HIGH52_MIN = -30.0
EPS_GROWTH_MIN = 20.0
REV_GROWTH_MIN = 15.0
MARKET_VAL_MIN = {"US": 15e9, "HK": 15e9, "A": 15e9}


def build_filters(market: str, ft):
    sf = ft.StockField
    ema = ft.StockField.EMA
    q = ft.FinancialQuarter.ANNUAL
    return [
        custom_indicator_filter(sf.PRICE, [], ema, [EMA_FAST]),
        custom_indicator_filter(ema, [EMA_FAST], ema, [EMA_MID]),
        custom_indicator_filter(ema, [EMA_MID], ema, [EMA_SLOW]),
        simple_filter(sf.CUR_PRICE_TO_LOWEST52_WEEKS_RATIO, LOW52_MIN),
        simple_filter(sf.CUR_PRICE_TO_HIGHEST52_WEEKS_RATIO, HIGH52_MIN),
        simple_filter(sf.MARKET_VAL, MARKET_VAL_MIN[market]),
        financial_filter(sf.EPS_GROWTH_RATE, EPS_GROWTH_MIN, quarter=q),
        financial_filter(sf.SUM_OF_BUSINESS_GROWTH, REV_GROWTH_MIN, quarter=q),
    ]


def score_snapshot(candidate, snap):
    # 按成交额排序；turnover 等字段已由 runner 平铺进 candidate
    return {"snapshot_score": num(snap.get("turnover")) or 0}


if __name__ == "__main__":
    main(sys.modules[__name__])
