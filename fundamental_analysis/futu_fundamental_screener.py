#  Futu Trends
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.

"""Shared Futu OpenD screener runner.

Strategy scripts own filter conditions and snapshot scoring.
This module owns CLI parsing, OpenD paging, snapshot enrichment, and JSON output.
"""

from __future__ import annotations

import argparse
import configparser
import json
import logging
import math
import sys
import time
from pathlib import Path
from typing import Any

import futu as ft

logging.getLogger("FTConsoleLog").setLevel(logging.WARNING)

MARKETS = ("US", "HK", "A")
FILTER_MARKETS = {
    "US": (ft.Market.US,),
    "HK": (ft.Market.HK,),
    # get_stock_filter 的 SH 即整个 A 股（含沪深），再查 SZ 会全量重复
    "A": (ft.Market.SH,),
}
# OTC(粉单)无行情权限，get_market_snapshot 会整批报错，需在 L1 后剔除
US_ALLOWED_EXCHANGES = {"US_NYSE", "US_NASDAQ", "US_AMEX"}
PAGE_SIZE = 200
SNAPSHOT_BATCH = 400
# futu 接口一般限制 1 分钟 30 次调用，节流间隔保持 >= 2s
FILTER_THROTTLE_SEC = 3.5
SNAPSHOT_THROTTLE_SEC = 2.5
FILTER_MAX_TRIES = 4
FILTER_BACKOFF_SEC = 6.0
YFINANCE_REFINE_LIMIT = 30
YFINANCE_SLEEP_SEC = 1.2

SNAPSHOT_FIELDS = (
    "last_price", "open_price", "high_price", "low_price", "prev_close_price",
    "turnover", "turnover_rate", "volume_ratio",
    "total_market_val", "net_asset", "net_profit", "earning_per_share",
    "net_asset_per_share", "pe_ratio", "pb_ratio", "pe_ttm_ratio",
    "dividend_ratio_ttm", "highest52weeks_price", "lowest52weeks_price",
    "suspension", "sec_status",
)

FILTER_FIELDS = (
    "market_val", "pe_ttm", "pb_rate", "return_on_equity_rate", "roa_ttm",
    "net_profit", "sum_of_business_growth", "net_profix_growth",
    "operating_cash_flow_ttm", "debt_asset_rate", "cash_and_cash_equivalents",
    "cur_price_to_lowest52_weeks_ratio", "cur_price_to_highest52_weeks_ratio",
    "eps_growth_rate",
)


def simple_filter(field, min_: float | None = None, max_: float | None = None,
                  sort=None):
    f = ft.SimpleFilter()
    f.stock_field = field
    if min_ is not None:
        f.filter_min = min_
    if max_ is not None:
        f.filter_max = max_
    if sort is not None:
        f.sort = sort
    f.is_no_filter = False
    return f


def accumulate_filter(field, min_: float | None = None, max_: float | None = None,
                      days: int = 5, sort=None):
    f = ft.AccumulateFilter()
    f.stock_field = field
    f.days = days
    if min_ is not None:
        f.filter_min = min_
    if max_ is not None:
        f.filter_max = max_
    if sort is not None:
        f.sort = sort
    f.is_no_filter = False
    return f


def financial_filter(field, min_: float | None = None, max_: float | None = None,
                     quarter=ft.FinancialQuarter.ANNUAL, sort=None):
    f = ft.FinancialFilter()
    f.stock_field = field
    f.quarter = quarter
    if min_ is not None:
        f.filter_min = min_
    if max_ is not None:
        f.filter_max = max_
    if sort is not None:
        f.sort = sort
    f.is_no_filter = False
    return f


def custom_indicator_filter(field1, para1, field2, para2,
                            relative=ft.RelativePosition.MORE,
                            ktype=ft.KLType.K_DAY):
    f = ft.CustomIndicatorFilter()
    f.ktype = ktype
    f.stock_field1 = field1
    f.stock_field1_para = para1
    f.relative_position = relative
    f.stock_field2 = field2
    f.stock_field2_para = para2
    f.is_no_filter = False
    return f


def num(value) -> float | None:
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def ratio(numerator, denominator, scale: float = 1.0) -> float | None:
    numerator, denominator = num(numerator), num(denominator)
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator * scale


def safe_inv(value, scale: float = 1.0) -> float | None:
    value = num(value)
    return scale / value if value and value > 0 else None


def safe_pct(value) -> float | None:
    value = num(value)
    return round(value * 100, 4) if value is not None else None


def row_value(row, name: str):
    d = row.__dict__
    if hasattr(row, name):
        return getattr(row, name)
    if name in d:
        return d[name]
    for key, value in d.items():
        if isinstance(key, tuple) and key[0] == name:
            return value
    return None


def _retry_call(desc: str, call, tries: int = FILTER_MAX_TRIES,
                backoff: float = FILTER_BACKOFF_SEC):
    """对 (ret, data) 形式的 OpenD 调用做指数退避重试（超时/限频多为瞬时故障）。"""
    data = None
    for attempt in range(1, tries + 1):
        ret, data = call()
        if ret == ft.RET_OK:
            return data
        if attempt < tries:
            wait = backoff * attempt
            print(f"[warn] {desc} failed, retry in {wait:.0f}s: {data}", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"{desc} failed: {data}")


def _filter_page(ctx, futu_market, filters: list[Any], begin: int):
    return _retry_call("get_stock_filter", lambda: ctx.get_stock_filter(
        market=futu_market, filter_list=filters, begin=begin, num=PAGE_SIZE,
    ))


def _candidate_from_filter_row(row, market: str) -> dict[str, Any]:
    out = {"code": row.stock_code, "name": row.stock_name, "market": market}
    out.update({field: row_value(row, field) for field in FILTER_FIELDS})
    return out


def _drop_us_otc(ctx, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # 不做"失败保留全部"兜底：混入 OTC 会让后续 get_market_snapshot 整批报错
    data = _retry_call("get_stock_basicinfo", lambda: ctx.get_stock_basicinfo(
        ft.Market.US, ft.SecurityType.STOCK))
    exchange = dict(zip(data["code"], data["exchange_type"]))
    kept = [c for c in candidates if exchange.get(c["code"]) in US_ALLOWED_EXCHANGES]
    if len(kept) < len(candidates):
        print(f"[info] dropped {len(candidates) - len(kept)} US OTC candidates",
              file=sys.stderr)
    return kept


def run_l1(strategy, market: str, config) -> list[dict[str, Any]]:
    host = config.get("CONFIG", "FUTU_HOST", fallback="127.0.0.1")
    port = int(config.get("CONFIG", "FUTU_PORT", fallback=11111))
    filters = strategy.build_filters(market, ft)

    ctx = ft.OpenQuoteContext(host=host, port=port)
    try:
        out, seen = [], set()
        futu_markets = FILTER_MARKETS[market]
        for market_idx, futu_market in enumerate(futu_markets):
            begin = 0
            while True:
                last_page, all_count, rows = _filter_page(ctx, futu_market, filters, begin)
                for row in rows:
                    if row.stock_code in seen:
                        continue
                    seen.add(row.stock_code)
                    if hasattr(strategy, "candidate_from_filter_row"):
                        out.append(strategy.candidate_from_filter_row(row, market))
                    else:
                        out.append(_candidate_from_filter_row(row, market))
                begin += len(rows)
                if last_page or not rows or begin >= all_count:
                    break
                time.sleep(FILTER_THROTTLE_SEC)
            if market_idx < len(futu_markets) - 1:
                time.sleep(FILTER_THROTTLE_SEC)
        if market == "US":
            out = _drop_us_otc(ctx, out)
        return out
    finally:
        ctx.close()


def default_snapshot_score(candidate: dict[str, Any], snap: dict[str, Any]) -> dict[str, Any]:
    turnover = num(snap.get("turnover")) or 0
    score = min(math.log10(turnover + 1) * 8, 80) if turnover else 0
    return {"snapshot_score": round(score, 3)}


def enrich_snapshot(candidates: list[dict[str, Any]], strategy, config) -> list[dict[str, Any]]:
    if not candidates:
        return candidates

    host = config.get("CONFIG", "FUTU_HOST", fallback="127.0.0.1")
    port = int(config.get("CONFIG", "FUTU_PORT", fallback=11111))
    codes = [c["code"] for c in candidates]
    snapshots: dict[str, dict[str, Any]] = {}

    ctx = ft.OpenQuoteContext(host=host, port=port)
    try:
        for i in range(0, len(codes), SNAPSHOT_BATCH):
            if i:
                time.sleep(SNAPSHOT_THROTTLE_SEC)
            batch = codes[i:i + SNAPSHOT_BATCH]
            data = _retry_call("get_market_snapshot",
                               lambda b=batch: ctx.get_market_snapshot(b))
            for row in data.to_dict("records"):
                snapshots[row["code"]] = row
    finally:
        ctx.close()

    scorer = getattr(strategy, "score_snapshot", default_snapshot_score)
    order: dict[str, float] = {}
    kept = []
    for candidate in candidates:
        snap = snapshots.get(candidate["code"], {})
        if snap.get("suspension"):
            continue  # 停牌股不可交易，剔除
        candidate.update({k: snap.get(k) for k in SNAPSHOT_FIELDS})
        last, prev = num(snap.get("last_price")), num(snap.get("prev_close_price"))
        candidate["change_pct"] = (
            round((last - prev) / prev * 100, 2) if last is not None and prev else None
        )
        metrics = dict(scorer(candidate, snap))
        # snapshot_score 仅用于排序，不落盘
        order[candidate["code"]] = metrics.pop("snapshot_score", None) or -1
        candidate.update(metrics)
        kept.append(candidate)

    if len(kept) < len(candidates):
        print(f"[info] dropped {len(candidates) - len(kept)} suspended candidates",
              file=sys.stderr)
    kept.sort(key=lambda c: order[c["code"]], reverse=True)
    return kept


def futu_to_yfinance_code(code: str) -> str:
    if code.startswith("HK."):
        return f"{code.split('.', 1)[1].lstrip('0') or '0'}.HK"
    if code.startswith("SH."):
        return f"{code.split('.', 1)[1]}.SS"
    if code.startswith("SZ."):
        return f"{code.split('.', 1)[1]}.SZ"
    if code.startswith("US."):
        return code.split(".", 1)[1]
    return code


def _statement_value(frame, names: tuple[str, ...], col: int = 0) -> float | None:
    if frame is None or getattr(frame, "empty", True) or frame.shape[1] <= col:
        return None
    for name in names:
        if name in frame.index:
            return num(frame.iloc[:, col].get(name))
    return None


def _statement_ratio_series(numerator_frame, numerator_names: tuple[str, ...],
                            denominator_frame, denominator_names: tuple[str, ...]) -> list[float]:
    if (
        numerator_frame is None or getattr(numerator_frame, "empty", True)
        or denominator_frame is None or getattr(denominator_frame, "empty", True)
    ):
        return []
    out = []
    columns = min(numerator_frame.shape[1], denominator_frame.shape[1])
    for col in range(columns):
        numerator = _statement_value(numerator_frame, numerator_names, col)
        denominator = _statement_value(denominator_frame, denominator_names, col)
        value = ratio(numerator, denominator)
        if value is not None:
            out.append(value)
    return out


def _positive(value) -> bool:
    value = num(value)
    return bool(value is not None and value > 0)


def _greater(a, b) -> bool:
    a, b = num(a), num(b)
    return bool(a is not None and b is not None and a > b)


def _yf_table(ticker, attr: str):
    table = getattr(ticker, attr, None)
    if table is None or getattr(table, "empty", True):
        return None
    return table


def _setup_yfinance(config):
    proxy = config.get("CONFIG", "PROXY", fallback=None)
    if not proxy:
        return
    if "://" not in proxy:
        proxy = f"http://{proxy}"

    # yfinance 1.x 走 curl_cffi，其 session 不读 HTTP(S)_PROXY 环境变量，
    # 也不受 requests monkey-patch 影响 —— 唯一可靠入口是 yf.config.network.proxy。
    # 因此这里以它为准，并校验是否真正生效，避免"配了代理却直连泄漏"。
    applied = False
    try:
        import yfinance as yf
        net = getattr(getattr(yf, "config", None), "network", None)
        if net is not None:
            net.proxy = proxy
            applied = getattr(net, "proxy", None) == proxy
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] yfinance proxy config failed: {exc}", file=sys.stderr)
    if not applied:
        print("[warn] yfinance 代理未生效（缺少 yf.config.network），L2 可能直连绕过代理",
              file=sys.stderr)

    # 仍设置全局 env/requests 代理：覆盖 requests-based 的取数路径（其它模块/子进程）。
    # 注意：这一步对 yfinance(curl_cffi) 无效，仅作旁路补充，不能替代上面的原生配置。
    try:
        try:
            from data import setup_global_proxy
        except ImportError:
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            from data import setup_global_proxy
        setup_global_proxy(proxy)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] setup_global_proxy failed: {exc}", file=sys.stderr)


def generic_yfinance_refine(candidate: dict[str, Any], yf_ticker) -> dict[str, Any]:
    income = _yf_table(yf_ticker, "income_stmt")
    balance = _yf_table(yf_ticker, "balance_sheet")
    cashflow = _yf_table(yf_ticker, "cash_flow")
    if income is None or balance is None:
        return {"ok": False, "note": "income_stmt or balance_sheet is empty"}

    roe_values = _statement_ratio_series(
        income,
        ("Net Income", "Net Income Common Stockholders"),
        balance,
        ("Stockholders Equity", "Common Stock Equity", "Total Equity Gross Minority Interest"),
    )
    roa_values = _statement_ratio_series(
        income,
        ("Net Income", "Net Income Common Stockholders"),
        balance,
        ("Total Assets",),
    )

    net_income = _statement_value(income, ("Net Income", "Net Income Common Stockholders"))
    operating_cf = _statement_value(cashflow, ("Operating Cash Flow", "Total Cash From Operating Activities"))
    total_assets = _statement_value(balance, ("Total Assets",))
    equity = _statement_value(balance, ("Stockholders Equity", "Common Stock Equity", "Total Equity Gross Minority Interest"))
    current_assets = _statement_value(balance, ("Current Assets",))
    current_liabilities = _statement_value(balance, ("Current Liabilities",))
    prev_current_assets = _statement_value(balance, ("Current Assets",), 1)
    prev_current_liabilities = _statement_value(balance, ("Current Liabilities",), 1)
    debt = _statement_value(balance, ("Long Term Debt", "Long Term Debt And Capital Lease Obligation"))
    prev_debt = _statement_value(balance, ("Long Term Debt", "Long Term Debt And Capital Lease Obligation"), 1)
    revenue = _statement_value(income, ("Total Revenue",))
    cogs = _statement_value(income, ("Cost Of Revenue", "Cost of Revenue"))
    prev_revenue = _statement_value(income, ("Total Revenue",), 1)
    prev_cogs = _statement_value(income, ("Cost Of Revenue", "Cost of Revenue"), 1)

    current_ratio = ratio(current_assets, current_liabilities)
    prev_current_ratio = ratio(prev_current_assets, prev_current_liabilities)
    gross_margin = ratio(revenue - cogs, revenue) if revenue is not None and cogs is not None else None
    prev_gross_margin = (
        ratio(prev_revenue - prev_cogs, prev_revenue)
        if prev_revenue is not None and prev_cogs is not None else None
    )
    latest_roa = ratio(net_income, total_assets)

    piotroski = 0
    flags = {}
    flags["positive_net_income"] = _positive(net_income)
    flags["positive_roa"] = _positive(latest_roa)
    flags["positive_operating_cash_flow"] = _positive(operating_cf)
    flags["cash_flow_gt_net_income"] = _greater(operating_cf, net_income)
    flags["lower_long_term_debt"] = bool(debt is not None and prev_debt is not None and debt < prev_debt)
    flags["higher_current_ratio"] = bool(
        current_ratio is not None and prev_current_ratio is not None
        and current_ratio > prev_current_ratio
    )
    flags["higher_gross_margin"] = bool(
        gross_margin is not None and prev_gross_margin is not None
        and gross_margin > prev_gross_margin
    )
    flags["has_valid_equity"] = bool(equity is not None and equity > 0)
    for value in flags.values():
        piotroski += int(bool(value))

    return {
        "ok": True,
        "yf_code": futu_to_yfinance_code(candidate["code"]),
        "periods": len(roe_values),
        "avg_roe_pct": safe_pct(sum(roe_values) / len(roe_values)) if roe_values else None,
        "min_roe_pct": safe_pct(min(roe_values)) if roe_values else None,
        "latest_roa_pct": safe_pct(latest_roa),
        "current_ratio": round(current_ratio, 4) if current_ratio is not None else None,
        "gross_margin_pct": safe_pct(gross_margin),
        "net_income": net_income,
        "operating_cash_flow": operating_cf,
        "piotroski_like_score": piotroski,
        "piotroski_like_flags": flags,
    }


def run_yfinance_refine(candidates: list[dict[str, Any]], strategy, config,
                        sleep_sec: float = YFINANCE_SLEEP_SEC) -> list[dict[str, Any]]:
    import yfinance as yf

    _setup_yfinance(config)
    refine_one = getattr(strategy, "refine_yfinance", generic_yfinance_refine)
    total = len(candidates)
    for i, candidate in enumerate(candidates, 1):
        code = candidate["code"]
        yf_code = futu_to_yfinance_code(code)
        print(f"[L2 {i}/{total}] {code} -> {yf_code}", file=sys.stderr)
        try:
            candidate["l2"] = refine_one(candidate, yf.Ticker(yf_code))
        except Exception as exc:  # noqa: BLE001
            candidate["l2"] = {"ok": False, "yf_code": yf_code, "note": str(exc)}
        if i < total and sleep_sec > 0:
            time.sleep(sleep_sec)
    return candidates


# ---- 结果存取协议（生产端与 gui/backend 共用，路径/格式的唯一定义处）----

OUT_ROOT = "output/screener"
_RESULT_GLOB = "*_*.json"


def result_path(root, date: str, strategy: str, market: str) -> Path:
    """协议路径：<root>/<YYYYMMDD>/<strategy>_<market>.json"""
    return Path(root) / date / f"{strategy}_{market}.json"


def write_result(path: Path, result: dict, date: str) -> None:
    """补 date/generated_at 后原子写入，避免 web 端读到半个文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    result = dict(result, date=date,
                  generated_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def resolve_date(root, on_or_before: str | None = None) -> str | None:
    """返回 <= on_or_before（默认今天）的最近一个有结果的日期。"""
    limit = on_or_before or time.strftime("%Y%m%d")
    root = Path(root)
    if not root.is_dir():
        return None
    dates = [d.name for d in root.iterdir()
             if d.is_dir() and len(d.name) == 8 and d.name.isdigit()
             and d.name <= limit and any(d.glob(_RESULT_GLOB))]
    return max(dates) if dates else None


def list_results(root, date: str) -> list[dict]:
    """某日期下全部结果的概要。"""
    out = []
    for f in sorted(Path(root, date).glob(_RESULT_GLOB)):
        strategy, _, market = f.stem.rpartition("_")
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append({"strategy": strategy, "market": market,
                    "generated_at": data.get("generated_at"),
                    "l1_count": data.get("l1_count"),
                    "returned": data.get("returned")})
    return out


def sanitize(obj):
    if hasattr(obj, "item"):
        return sanitize(obj.item())
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize(v) for v in obj]
    return obj


def screen(strategy, market: str, config, snapshot: bool = True,
           limit: int | None = None, refine: bool = False,
           refine_limit: int | None = YFINANCE_REFINE_LIMIT,
           refine_sleep: float = YFINANCE_SLEEP_SEC) -> dict[str, Any]:
    if limit is not None and limit < 0:
        raise ValueError("limit must be >= 0")
    if refine_limit is not None and refine_limit < 0:
        raise ValueError("refine_limit must be >= 0")

    candidates = run_l1(strategy, market, config)
    l1_count = len(candidates)
    if snapshot:
        candidates = enrich_snapshot(candidates, strategy, config)
    if limit is not None:
        candidates = candidates[:limit]
    l2_refined = 0
    l2_filtered = False
    if refine:
        refine_count = len(candidates) if refine_limit is None else min(refine_limit, len(candidates))
        if refine_count:
            run_yfinance_refine(candidates[:refine_count], strategy, config, refine_sleep)
        l2_refined = refine_count
        # 定义了 l2_passes 的策略在 --refine 时直接按其门槛过滤；未定义则仅注释不过滤。
        passes = getattr(strategy, "l2_passes", None)
        if passes is not None:
            if l2_refined < len(candidates):
                print(f"[warn] L2 过滤：仅精算了前 {l2_refined}/{len(candidates)} 只，"
                      f"未精算的将被剔除；如需全量请调大 --refine-limit", file=sys.stderr)
            candidates = [c for c in candidates if passes(c)]
            l2_filtered = True
    return sanitize({
        "market": market,
        "strategy": strategy.NAME,
        "l1_count": l1_count,
        "snapshot_enriched": bool(snapshot),
        "l2_refined": l2_refined,
        "l2_filtered": l2_filtered,
        "returned": len(candidates),
        "candidates": candidates,
    })


def parser(strategy) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=getattr(strategy, "DESCRIPTION", "Futu OpenD screener"),
    )
    ap.add_argument("--market", required=True, choices=list(MARKETS))
    ap.add_argument("--config", default="config_template.ini")
    ap.add_argument("--limit", type=int, help="按 snapshot_score 排序后的输出数量")
    ap.add_argument("--no-snapshot", action="store_true", help="只跑 get_stock_filter")
    ap.add_argument("--refine", action="store_true", help="对候选运行 yfinance L2 精算")
    ap.add_argument("--refine-limit", type=int, default=YFINANCE_REFINE_LIMIT,
                    help=f"最多精算前多少只，不截断返回列表；默认 {YFINANCE_REFINE_LIMIT}")
    ap.add_argument("--refine-sleep", type=float, default=YFINANCE_SLEEP_SEC,
                    help=f"yfinance 单只间隔秒数；默认 {YFINANCE_SLEEP_SEC}")
    ap.add_argument("--out", help="输出 JSON 文件")
    ap.add_argument("--out-root", help=f"按协议路径输出：<root>/<date>/<strategy>_<market>.json，如 {OUT_ROOT}")
    ap.add_argument("--date", help="配合 --out-root 的结果日期 YYYYMMDD，默认今天")
    return ap


def main(strategy):
    args = parser(strategy).parse_args()
    config = configparser.ConfigParser()
    if not config.read(args.config, encoding="utf-8"):
        sys.exit(f"配置文件读取失败: {args.config}")
    if not config.has_section("CONFIG"):
        sys.exit(f"配置缺少 [CONFIG] 段: {args.config}")

    result = screen(
        strategy, args.market, config, snapshot=not args.no_snapshot,
        limit=args.limit, refine=args.refine, refine_limit=args.refine_limit,
        refine_sleep=args.refine_sleep,
    )
    if args.out_root:
        date = args.date or time.strftime("%Y%m%d")
        path = result_path(args.out_root, date, strategy.NAME, args.market)
        write_result(path, result, date)
        print(f"已写入 {path}（L1={result['l1_count']}, 返回={result['returned']}）",
              file=sys.stderr)
        return
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"已写入 {args.out}（L1={result['l1_count']}, 返回={result['returned']}）",
              file=sys.stderr)
    else:
        print(text)
