"""Deep-value fundamental screener."""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from futu_fundamental_screener import (  # noqa: E402
    FutuFinancials,
    accumulate_filter,
    financial_filter,
    main,
    num,
    ratio,
    safe_inv,
    simple_filter,
)

NAME = "deep_value"
DESCRIPTION = "Deep-value fundamental screener"
FUTU_L2_STATEMENT_TYPES = ("income", "balance")

MARKET_CAP_MIN = 1e9
TURNOVER_AVG_DAYS = 20
TURNOVER_MIN = {"US": 50e6, "HK": 5e6, "A": 50e6}
PE_MIN = 0.01
PE_MAX = 13.0
PB_MAX = 1.0
CASH_MIN = 0.0
DEBT_ASSET_MAX = 50.0
# Futu V1 CURRENT_RATIO uses percentage points: 150 means 1.5x.
CURRENT_RATIO_MIN_PCT = 150.0


def build_filters(market: str, ft):
    sf = ft.StockField
    q = ft.FinancialQuarter.ANNUAL
    return [
        simple_filter(sf.MARKET_VAL, MARKET_CAP_MIN),
        accumulate_filter(
            sf.TURNOVER, TURNOVER_MIN[market], days=TURNOVER_AVG_DAYS,
        ),
        simple_filter(sf.PE_TTM, PE_MIN, PE_MAX),
        simple_filter(sf.PB_RATE, 0.01, PB_MAX, sort=ft.SortDir.ASCEND),
        financial_filter(sf.NET_PROFIT, 0, quarter=q),
        financial_filter(sf.CASH_AND_CASH_EQUIVALENTS, CASH_MIN, quarter=q),
        financial_filter(sf.DEBT_ASSET_RATE, max_=DEBT_ASSET_MAX, quarter=q),
        financial_filter(sf.CURRENT_RATIO, CURRENT_RATIO_MIN_PCT, quarter=q),
    ]


def score_snapshot(candidate, snap):
    turnover = num(snap.get("turnover")) or 0
    roe = ratio(snap.get("net_profit"), snap.get("net_asset"), 100) or 0
    earnings_yield = safe_inv(snap.get("pe_ttm_ratio") or snap.get("pe_ratio"), 100) or 0
    book_discount = safe_inv(snap.get("pb_ratio")) or 0
    dividend = num(snap.get("dividend_ratio_ttm")) or 0
    liquidity = min(math.log10(turnover + 1) * 8, 80) if turnover else 0
    score = book_discount * 25 + earnings_yield * 2 + dividend + liquidity * 0.25
    return {"snapshot_roe": round(roe, 4), "snapshot_score": round(score, 3)}


def refine_futu(candidate, financials: FutuFinancials):
    latest = financials.latest("income", "balance")
    if not latest:
        return {
            "ok": False,
            "source": "futu",
            "note": "no common report period across income and balance",
            "latest_available_periods": financials.latest_available_periods(),
        }
    income, balance = latest
    fields = financials.fields_for(income, balance)

    # 富途市值使用交易币种；若报表币种不同，市值与 NCAV 不可直接比较。
    # 仅在币种均已知且不一致时剔除。
    financial_currency = income.currency_code or balance.currency_code
    trade_currency = {
        "US": "USD", "HK": "HKD", "SH": "CNY", "SZ": "CNY",
    }.get(candidate["code"].split(".", 1)[0])
    currency_ok = not (financial_currency and trade_currency
                       and financial_currency != trade_currency)

    net_income = income.value(fields.net_income)
    cash = balance.value(fields.cash)
    total_liabilities = balance.value(fields.total_liabilities)
    current_assets = balance.value(fields.current_assets)
    interest_debt = balance.sum_values(fields.total_debt_components)
    equity = balance.value(fields.equity)
    market_cap = num(candidate.get("total_market_val")) or num(candidate.get("market_val"))
    metadata = {
        "source": "futu",
        "report_period": income.period_text,
        "report_date": income.date_time_str,
        "financial_type": income.financial_type,
        "financial_currency": financial_currency,
        "trade_currency": trade_currency,
        "accounting_standards": (
            income.accounting_standards or balance.accounting_standards
        ),
        "latest_available_periods": financials.latest_available_periods(),
    }
    missing_fields = [
        name for name, value in (
            ("net_income", net_income),
            ("cash", cash),
            ("current_assets", current_assets),
            ("total_liabilities", total_liabilities),
            ("equity", equity),
            ("market_cap", market_cap),
        )
        if value is None
    ]
    if missing_fields:
        return {
            "ok": False,
            **metadata,
            "note": "required Futu field_id is missing",
            "missing_fields": missing_fields,
        }

    net_cash = None
    if cash is not None and total_liabilities is not None:
        net_cash = cash - total_liabilities
    ncav = None
    if current_assets is not None and total_liabilities is not None:
        ncav = current_assets - total_liabilities

    return {
        "ok": True,
        **metadata,
        "net_income": net_income,
        "cash_and_equivalents": cash,
        "current_assets": current_assets,
        "total_liabilities": total_liabilities,
        "interest_bearing_debt": interest_debt,
        "shareholders_equity": equity,
        "market_cap": market_cap,
        "net_cash": net_cash,
        "ncav": ncav,
        "ncav_to_market_cap": ratio(ncav, market_cap),
        "cash_to_market_cap": ratio(cash, market_cap),
        "cash_to_liabilities": ratio(cash, total_liabilities),
        "debt_to_equity": ratio(interest_debt, equity),
        # 报表币种与交易币种一致才可做 NCAV 比较
        "condition_currency_ok": currency_ok,
        # Graham 烟蒂：市值 < NCAV（流动资产 − 总负债）；币种不一致直接判否
        "condition_market_cap_lt_ncav": bool(
            currency_ok and ncav is not None and market_cap is not None and market_cap < ncav
        ),
        # 经典买点：市值 < 2/3 NCAV
        "condition_market_cap_lt_two_thirds_ncav": bool(
            currency_ok and ncav is not None and market_cap is not None and market_cap < ncav * 2 / 3
        ),
        "condition_cash_minus_liabilities_gt_market_cap": bool(
            currency_ok and net_cash is not None
            and market_cap is not None and net_cash > market_cap
        ),
        "condition_cash_minus_debt_gt_liabilities": bool(
            cash is not None and interest_debt is not None
            and total_liabilities is not None and cash - interest_debt > total_liabilities
        ),
    }


def l2_passes(candidate) -> bool:
    """--refine L2 门槛：剔除币种不一致者，仅保留市值 < NCAV。"""
    l2 = candidate.get("l2") or {}
    return bool(
        l2.get("ok")
        and l2.get("condition_currency_ok")
        and l2.get("condition_market_cap_lt_ncav")
    )


if __name__ == "__main__":
    main(sys.modules[__name__])
