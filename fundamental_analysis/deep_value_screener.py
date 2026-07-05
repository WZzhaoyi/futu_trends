"""Deep-value fundamental screener."""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from futu_fundamental_screener import (  # noqa: E402
    _statement_value,
    financial_filter,
    futu_to_yfinance_code,
    main,
    num,
    ratio,
    safe_inv,
    simple_filter,
)

NAME = "deep_value"
DESCRIPTION = "Deep-value fundamental screener"

MARKET_CAP_MIN = 1e9
PE_MIN = 0.01
PE_MAX = 13.0
PB_MAX = 1.0
CASH_MIN = 0.0
DEBT_ASSET_MAX = 50.0
CURRENT_RATIO_MIN = 1.5


def build_filters(market: str, ft):
    sf = ft.StockField
    q = ft.FinancialQuarter.ANNUAL
    return [
        simple_filter(sf.MARKET_VAL, MARKET_CAP_MIN),
        simple_filter(sf.PE_TTM, PE_MIN, PE_MAX),
        simple_filter(sf.PB_RATE, 0.01, PB_MAX, sort=ft.SortDir.ASCEND),
        financial_filter(sf.NET_PROFIT, 0, quarter=q),
        financial_filter(sf.CASH_AND_CASH_EQUIVALENTS, CASH_MIN, quarter=q),
        financial_filter(sf.DEBT_ASSET_RATE, max_=DEBT_ASSET_MAX, quarter=q),
        financial_filter(sf.CURRENT_RATIO, CURRENT_RATIO_MIN, quarter=q),
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


def refine_yfinance(candidate, yf_ticker):
    income = yf_ticker.income_stmt
    balance = yf_ticker.balance_sheet
    if income is None or income.empty or balance is None or balance.empty:
        return {"ok": False, "note": "income_stmt or balance_sheet is empty"}

    # 美股中概/ADR：报表币种(如 CNY/EUR)与交易币种(USD)不一致时，
    # 富途市值(USD)与 yfinance 报表口径的 NCAV 不可比，需剔除。仅在"已知不一致"时剔除。
    try:
        info = getattr(yf_ticker, "info", None) or {}
    except Exception:  # noqa: BLE001
        info = {}
    financial_currency = info.get("financialCurrency")
    trade_currency = info.get("currency")
    currency_ok = not (financial_currency and trade_currency
                       and financial_currency != trade_currency)

    net_income = _statement_value(income, ("Net Income", "Net Income Common Stockholders"))
    cash = _statement_value(
        balance,
        ("Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"),
    )
    total_liabilities = _statement_value(
        balance,
        ("Total Liabilities Net Minority Interest", "Total Liabilities"),
    )
    current_assets = _statement_value(balance, ("Current Assets",))
    interest_debt = _statement_value(
        balance,
        ("Total Debt", "Long Term Debt", "Long Term Debt And Capital Lease Obligation"),
    )
    equity = _statement_value(
        balance,
        ("Stockholders Equity", "Common Stock Equity", "Total Equity Gross Minority Interest"),
    )
    market_cap = num(candidate.get("total_market_val")) or num(candidate.get("market_val"))
    net_cash = None
    if cash is not None and total_liabilities is not None:
        net_cash = cash - total_liabilities
    ncav = None
    if current_assets is not None and total_liabilities is not None:
        ncav = current_assets - total_liabilities

    return {
        "ok": True,
        "yf_code": futu_to_yfinance_code(candidate["code"]),
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
        "debt_to_equity": ratio(total_liabilities, equity),
        "financial_currency": financial_currency,
        "trade_currency": trade_currency,
        # 报表币种与交易币种一致才可做 NCAV 比较（剔除美股中概/ADR）
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
            net_cash is not None and market_cap is not None and net_cash > market_cap
        ),
        "condition_cash_minus_debt_gt_liabilities": bool(
            cash is not None and interest_debt is not None
            and total_liabilities is not None and cash - interest_debt > total_liabilities
        ),
    }


def l2_passes(candidate) -> bool:
    """--refine L2 门槛：剔除美股中概/ADR（币种不一致），仅保留命中 Graham 烟蒂（市值 < NCAV）的候选。"""
    l2 = candidate.get("l2") or {}
    return bool(
        l2.get("ok")
        and l2.get("condition_currency_ok")
        and l2.get("condition_market_cap_lt_ncav")
    )


if __name__ == "__main__":
    main(sys.modules[__name__])
