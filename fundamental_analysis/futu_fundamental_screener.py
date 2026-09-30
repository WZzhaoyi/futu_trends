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
import threading
import time
from dataclasses import dataclass
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

# ---- 「要求为正」的下限 ----
# Futu 的 filter_min 是闭区间，且实测对 1e-9 及以下的下限会量化成 0：
# US 市场单独用 OCF_TTM >= 0 / >= 1e-12 / >= 1e-9 三条筛选，命中数完全相同
# （5054），而 >= 0.001 / >= 0.01 / >= 1 也都是 4864。
# 所以「用极小正数表达严格 >0」是无效的写法，POSITIVE_MIN 取 0.01 才真正生效。
# 它在比率/倍数（PE、PB、权益乘数）上是「近似 0」，在货币金额上是「1 分钱」，
# 两者语义都仍是桶底；但**百分比口径上它是 0.01%，不是「近似 0」**——
# 百分比字段里带业务含义的零阈值（如「增速 ≥0 即不收缩」）不要套用它。
POSITIVE_MIN = 0.01
SNAPSHOT_BATCH = 400
# futu 接口一般限制 1 分钟 30 次调用，节流间隔保持 >= 2s
FILTER_THROTTLE_SEC = 3.5
SNAPSHOT_THROTTLE_SEC = 2.5
# GetFinancialsStatements: 30 秒最多 30 次；1.1 秒固定间隔保留少量余量。
FINANCIAL_THROTTLE_SEC = 1.1
# 三表统一采用累计口径（Q1/H1/Q9/FY），避免单季利润表与累计现金流错配。
# 25 期通常可覆盖 5 个完整年报，且不增加调用次数。
FINANCIAL_PERIODS = 25
FINANCIAL_CUMULATIVE_TYPE = 11
FINANCIAL_SINGLE_QUARTER_TYPE = 10
FINANCIAL_ANNUAL_TYPE = 7

FINANCIAL_ALIGNMENT_TYPES = {
    1: 1,       # Q1
    2: 5, 5: 5, # Q2 单季 / H1 累计
    3: 6, 6: 6, # Q3 单季 / Q9 累计
    4: 7, 7: 7, # Q4 单季 / FY
}

_financial_lock = threading.Lock()

FINANCIAL_STATEMENT_TYPES = {
    "income": 1,
    "balance": 2,
    "cashflow": 3,
}

SNAPSHOT_FIELDS = (
    "last_price", "open_price", "high_price", "low_price", "prev_close_price",
    "turnover", "turnover_rate", "volume_ratio",
    "total_market_val", "net_asset", "net_profit", "earning_per_share",
    "net_asset_per_share", "pe_ratio", "pb_ratio", "pe_ttm_ratio",
    "dividend_ratio_ttm", "highest52weeks_price", "lowest52weeks_price",
    "suspension", "sec_status",
)

FILTER_FIELDS = (
    "market_val", "pcf_ttm", "pe_ttm", "pb_rate", "return_on_equity_rate", "roa_ttm",
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
                     quarter=ft.FinancialQuarter.ANNUAL, sort=None,
                     is_no_filter: bool = False):
    f = ft.FinancialFilter()
    f.stock_field = field
    f.quarter = quarter
    if min_ is not None:
        f.filter_min = min_
    if max_ is not None:
        f.filter_max = max_
    if sort is not None:
        f.sort = sort
    f.is_no_filter = is_no_filter
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


def _futu_call(desc: str, call):
    """执行返回 (ret, data) 的 OpenD 调用，并在接口边界检查结果。"""
    ret, data = call()
    if ret != ft.RET_OK:
        raise RuntimeError(f"{desc} failed: {data}")
    return data


def _filter_page(ctx, futu_market, filters: list[Any], begin: int):
    return _futu_call("get_stock_filter", lambda: ctx.get_stock_filter(
        market=futu_market, filter_list=filters, begin=begin, num=PAGE_SIZE,
    ))


def _candidate_from_filter_row(row, market: str) -> dict[str, Any]:
    out = {"code": row.stock_code, "name": row.stock_name, "market": market}
    out.update({field: row_value(row, field) for field in FILTER_FIELDS})
    return out


def _drop_us_otc(ctx, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # 不做"失败保留全部"兜底：混入 OTC 会让后续 get_market_snapshot 整批报错
    data = _futu_call("get_stock_basicinfo", lambda: ctx.get_stock_basicinfo(
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
                        candidate = strategy.candidate_from_filter_row(row, market)
                    else:
                        candidate = _candidate_from_filter_row(row, market)
                    if candidate is not None:
                        out.append(candidate)
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
            data = _futu_call("get_market_snapshot",
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


@dataclass(frozen=True)
class FutuFieldSet:
    """Futu F10 原生 field_id；每个 ID 的语义同时受报表类型约束。"""

    net_income: tuple[int, ...]
    revenue: tuple[int, ...]
    cost_of_revenue: tuple[int, ...]
    gross_profit: tuple[int, ...]
    total_assets: tuple[int, ...]
    equity: tuple[int, ...]
    current_assets: tuple[int, ...]
    current_liabilities: tuple[int, ...]
    cash: tuple[int, ...]
    total_liabilities: tuple[int, ...]
    long_term_debt_components: tuple[int, ...]
    total_debt_components: tuple[int, ...]
    operating_cash_flow: tuple[int, ...]
    # ---- 现金流表：固定资产相关（用于 FCF = 经营现金流 − 净资本开支）----
    # 符号口径按市场不同，务必配合 FUTU_FIXED_ASSET_SIGN 使用，详见其注释。
    fixed_asset_acquired: tuple[int, ...] = ()
    fixed_asset_disposed: tuple[int, ...] = ()
    fixed_asset_net: tuple[int, ...] = ()


# Futu 的财报字段不是 SDK 枚举：OpenD 返回 field_id + display_name。L2 只使用稳定的
# field_id，display_name 仅供 UI 展示，不参与计算，因此不受 OpenD 语言影响。
# 3xxx/5xxx/8xxx 分别是当前选股范围内的 A/HK/US 财报字段族。
FUTU_FIELD_SETS = {
    3: FutuFieldSet(
        net_income=(3043, 3047),
        revenue=(3002, 3001),
        cost_of_revenue=(3010, 3009),
        gross_profit=(),
        total_assets=(3001,),
        equity=(3097, 3098),
        current_assets=(3002,),
        current_liabilities=(3056,),
        cash=(3003,),
        total_liabilities=(3055,),
        long_term_debt_components=(3084, 3085, 3087),
        total_debt_components=(3067, 3075, 3084, 3085, 3087),
        operating_cash_flow=(3001,),
        # A 股：3043「购建固定资产…支付的现金」(正号=流出)、3037「处置…收回的现金净额」(正号=流入)
        fixed_asset_acquired=(3043,),
        fixed_asset_disposed=(3037,),
    ),
    5: FutuFieldSet(
        net_income=(5045, 5051, 5052),
        revenue=(5002, 5001),
        cost_of_revenue=(5008, 5005),
        gross_profit=(5010,),
        total_assets=(5001,),
        equity=(5109, 5110),
        current_assets=(5002,),
        current_liabilities=(5061,),
        cash=(5003,),
        total_liabilities=(5060,),
        long_term_debt_components=(5091, 5093, 5104),
        total_debt_components=(5070, 5072, 5091, 5093, 5104),
        operating_cash_flow=(5001,),
        # 港股：5071「购买固定资产」(本身为负号)、5070「出售固定资产」(正号)
        fixed_asset_acquired=(5071,),
        fixed_asset_disposed=(5070,),
    ),
    8: FutuFieldSet(
        net_income=(8037, 8043, 8046),
        revenue=(8002, 8001),
        cost_of_revenue=(8003,),
        gross_profit=(8004,),
        total_assets=(8001,),
        equity=(8081, 8085),
        current_assets=(8002,),
        current_liabilities=(8049,),
        cash=(8003, 8004),
        total_liabilities=(8048,),
        long_term_debt_components=(8068,),
        total_debt_components=(8057, 8068),
        operating_cash_flow=(8015, 8016),
        # 美股：Futu 只给 8046「固定资产交易净额」，已含处置且已带符号（可为正）。
        # 注意同一 id 在利润表里是别的科目，必须只从 cashflow 报表读取。
        fixed_asset_net=(8046,),
    ),
}


@dataclass(frozen=True)
class FutuFinancialReport:
    statement_name: str
    date_time_str: str
    financial_type: int
    period_text: str
    currency_code: str
    accounting_standards: str
    values: dict[int, float]

    @property
    def alignment_key(self) -> tuple[str, int]:
        return (
            self.date_time_str,
            FINANCIAL_ALIGNMENT_TYPES.get(self.financial_type, self.financial_type),
        )

    @property
    def family(self) -> int:
        families = {field_id // 1000 for field_id in self.values if field_id > 0}
        known = families & set(FUTU_FIELD_SETS)
        if len(known) != 1:
            raise ValueError(
                f"unsupported Futu F10 field family for {self.statement_name}: "
                f"{sorted(families)}"
            )
        return next(iter(known))

    def value(self, field_ids: tuple[int, ...]) -> float | None:
        for field_id in field_ids:
            value = self.values.get(field_id)
            if value is not None:
                return value
        return None

    def sum_values(self, field_ids: tuple[int, ...]) -> float:
        # F10 对零余额科目通常不下发 item；已识别字段族后，缺失的债务分项按 0 处理。
        return sum(self.values.get(field_id, 0.0) for field_id in field_ids)


def _parse_financial_reports(statement_name: str,
                             data: dict[str, Any]) -> tuple[FutuFinancialReport, ...]:
    """直接解析 SDK report_list；display_name/structure_list 不进入 L2 数据模型。"""
    reports = []
    for raw in data.get("report_list", []):
        values = {}
        for item in raw.get("item_list", []):
            try:
                field_id = int(item.get("field_id"))
            except (TypeError, ValueError):
                continue
            value = num(item.get("data"))
            if field_id > 0 and value is not None:
                values[field_id] = value
        date = str(raw.get("date_time_str") or "")
        financial_type = int(raw.get("financial_type") or 0)
        if not date or financial_type <= 0:
            continue
        reports.append(FutuFinancialReport(
            statement_name=statement_name,
            date_time_str=date,
            financial_type=financial_type,
            period_text=str(raw.get("period_text") or date),
            currency_code=str(raw.get("currency_code") or ""),
            accounting_standards=str(raw.get("accounting_standards") or ""),
            values=values,
        ))
    reports.sort(
        key=lambda report: (
            report.date_time_str,
            report.financial_type == FINANCIAL_ANNUAL_TYPE,
            report.financial_type,
        ),
        reverse=True,
    )
    return tuple(reports)


class FutuFinancials:
    """一只标的的 Futu F10 报表集合，负责报告期对齐与字段族校验。"""

    def __init__(self, statements: dict[str, tuple[FutuFinancialReport, ...]]):
        self.statements = statements

    def aligned(self, *statement_names: str,
                financial_type: int | None = None) -> list[tuple[FutuFinancialReport, ...]]:
        if not statement_names:
            return []
        mappings = []
        for name in statement_names:
            reports = self.statements.get(name, ())
            if financial_type is not None:
                alignment_type = FINANCIAL_ALIGNMENT_TYPES.get(
                    financial_type, financial_type,
                )
                reports = tuple(
                    report for report in reports
                    if FINANCIAL_ALIGNMENT_TYPES.get(
                        report.financial_type, report.financial_type,
                    ) == alignment_type
                )
            mapping = {}
            for report in reports:
                # 同一截止日可能同时有 Q4 和 FY；解析结果已将 FY 排在前面。
                mapping.setdefault(report.alignment_key, report)
            mappings.append(mapping)
        if any(not mapping for mapping in mappings):
            return []
        keys = set(mappings[0])
        for mapping in mappings[1:]:
            keys &= set(mapping)
        ordered_keys = sorted(
            keys,
            key=lambda key: (key[0], key[1] == FINANCIAL_ANNUAL_TYPE, key[1]),
            reverse=True,
        )
        return [tuple(mapping[key] for mapping in mappings) for key in ordered_keys]

    def latest(self, *statement_names: str) -> tuple[FutuFinancialReport, ...] | None:
        aligned = self.aligned(*statement_names)
        return aligned[0] if aligned else None

    def previous_comparable(self, current: FutuFinancialReport,
                            *statement_names: str) -> tuple[FutuFinancialReport, ...] | None:
        aligned = self.aligned(*statement_names, financial_type=current.financial_type)
        for reports in aligned:
            if reports[0].date_time_str < current.date_time_str:
                return reports
        return None

    def ttm(self, statement_name: str, field_ids: tuple[int, ...]) -> float | None:
        """把累计口径报表拼成 TTM：最近一期 + 上一完整年度 − 去年同期同口径。

        年报本身即 TTM，直接取用。任一组成项缺失即返回 None —— 宁可判「不可计算」，
        也不要用半截口径算出一个偏小或偏大的分母。
        """
        reports = sorted(
            self.statements.get(statement_name, ()),
            key=lambda report: report.date_time_str, reverse=True,
        )
        if not reports or not field_ids:
            return None
        latest = reports[0]
        alignment = FINANCIAL_ALIGNMENT_TYPES.get(
            latest.financial_type, latest.financial_type,
        )
        if alignment == FINANCIAL_ANNUAL_TYPE:
            return latest.value(field_ids)
        annual = [
            report for report in reports
            if FINANCIAL_ALIGNMENT_TYPES.get(
                report.financial_type, report.financial_type,
            ) == FINANCIAL_ANNUAL_TYPE and report.date_time_str < latest.date_time_str
        ]
        prior_same = [
            report for report in reports
            if FINANCIAL_ALIGNMENT_TYPES.get(
                report.financial_type, report.financial_type,
            ) == alignment and report.date_time_str < latest.date_time_str
        ]
        if not annual or not prior_same:
            return None
        current_value = latest.value(field_ids)
        annual_value = annual[0].value(field_ids)
        prior_value = prior_same[0].value(field_ids)
        if current_value is None or annual_value is None or prior_value is None:
            return None
        return current_value + annual_value - prior_value

    def net_fixed_asset_cash_flow_ttm(self, fields: FutuFieldSet) -> float | None:
        """固定资产交易的净现金流（TTM，负号=净流出），即 FCF 里的「−资本开支」。

        符号口径三地不同：A 的 3043 是「正号流出」、港股的 5071 是「负号流出」，
        取绝对值后两者归一；美股的 8046 本身已是净额且已带符号，直接用。
        """
        if fields.fixed_asset_net:
            return self.ttm("cashflow", fields.fixed_asset_net)
        acquired = self.ttm("cashflow", fields.fixed_asset_acquired)
        disposed = self.ttm("cashflow", fields.fixed_asset_disposed)
        if acquired is None:
            return None
        return (disposed or 0.0) - abs(acquired)

    def fields_for(self, *reports: FutuFinancialReport) -> FutuFieldSet:
        families = {report.family for report in reports}
        if len(families) != 1:
            raise ValueError(f"Futu F10 field families do not match: {sorted(families)}")
        return FUTU_FIELD_SETS[next(iter(families))]

    def latest_available_periods(self) -> dict[str, str | None]:
        return {
            name: reports[0].period_text if reports else None
            for name, reports in self.statements.items()
        }


def _financial_query_type(code: str, statement_name: str) -> int:
    # 美股资产负债表按 Q1/Q2/Q3/FY 保存；利润与现金流按 Q1/H1/Q9/FY 累计。
    if code.startswith("US.") and statement_name == "balance":
        return FINANCIAL_SINGLE_QUARTER_TYPE
    return FINANCIAL_CUMULATIVE_TYPE


def _financial_api_call(ctx, *, code: str, statement_type: int,
                        financial_type: int) -> dict[str, Any]:
    """串行调用 F10，并用固定间隔满足 30 次/30 秒的接口限制。"""
    try:
        ret, data = ctx.get_financials_statements(
            code,
            statement_type=statement_type,
            financial_type=financial_type,
            num=FINANCIAL_PERIODS,
        )
    finally:
        time.sleep(FINANCIAL_THROTTLE_SEC)
    if ret != ft.RET_OK:
        raise RuntimeError(f"get_financials_statements {code}/{statement_type} failed: {data}")
    return data if isinstance(data, dict) else {}


def _fetch_financial_statement(ctx, cache: dict, code: str,
                               statement_name: str) -> tuple[FutuFinancialReport, ...]:
    financial_type = _financial_query_type(code, statement_name)
    cache_key = (code, statement_name, financial_type)
    if cache_key not in cache:
        data = _financial_api_call(
            ctx, code=code,
            statement_type=FINANCIAL_STATEMENT_TYPES[statement_name],
            financial_type=financial_type,
        )
        cache[cache_key] = _parse_financial_reports(statement_name, data)
    return cache[cache_key]


def _positive(value) -> bool:
    value = num(value)
    return bool(value is not None and value > 0)


def _greater(a, b) -> bool:
    a, b = num(a), num(b)
    return bool(a is not None and b is not None and a > b)


def _gross_margin(income_report: FutuFinancialReport | None,
                  fields: FutuFieldSet) -> float | None:
    if income_report is None:
        return None
    revenue = income_report.value(fields.revenue)
    gross_profit = income_report.value(fields.gross_profit)
    if gross_profit is not None:
        return ratio(gross_profit, revenue)
    cost = income_report.value(fields.cost_of_revenue)
    if revenue is None or cost is None:
        return None
    # 部分 Futu 会计模板以负数返回费用。
    return ratio(revenue + cost if cost < 0 else revenue - cost, revenue)


def generic_futu_refine(candidate: dict[str, Any],
                        financials: FutuFinancials) -> dict[str, Any]:
    latest = financials.latest("income", "balance", "cashflow")
    if not latest:
        return {
            "ok": False,
            "source": "futu",
            "note": "no common report period across income, balance and cashflow",
            "latest_available_periods": financials.latest_available_periods(),
        }
    income, balance, cashflow = latest
    fields = financials.fields_for(income, balance, cashflow)

    roe_values = []
    annual = financials.aligned(
        "income", "balance", financial_type=FINANCIAL_ANNUAL_TYPE,
    )
    for income_report, balance_report in annual:
        annual_fields = financials.fields_for(income_report, balance_report)
        value = ratio(
            income_report.value(annual_fields.net_income),
            balance_report.value(annual_fields.equity),
        )
        if value is not None:
            roe_values.append(value)

    previous = financials.previous_comparable(income, "income", "balance")
    prev_income, prev_balance = previous if previous else (None, None)

    net_income = income.value(fields.net_income)
    operating_cf = cashflow.value(fields.operating_cash_flow)
    total_assets = balance.value(fields.total_assets)
    equity = balance.value(fields.equity)
    current_ratio = ratio(
        balance.value(fields.current_assets),
        balance.value(fields.current_liabilities),
    )
    prev_current_ratio = ratio(
        prev_balance.value(fields.current_assets) if prev_balance else None,
        prev_balance.value(fields.current_liabilities) if prev_balance else None,
    )
    debt = balance.sum_values(fields.long_term_debt_components)
    prev_debt = (
        prev_balance.sum_values(fields.long_term_debt_components)
        if prev_balance else None
    )
    gross_margin = _gross_margin(income, fields)
    prev_gross_margin = _gross_margin(prev_income, fields)
    latest_roa = ratio(net_income, total_assets)

    missing_fields = [
        name for name, value in (
            ("net_income", net_income),
            ("operating_cash_flow", operating_cf),
            ("total_assets", total_assets),
            ("equity", equity),
        )
        if value is None
    ]
    metadata = {
        "source": "futu",
        "report_period": income.period_text,
        "report_date": income.date_time_str,
        "financial_type": income.financial_type,
        "financial_currency": income.currency_code or balance.currency_code,
        "accounting_standards": (
            income.accounting_standards or balance.accounting_standards
        ),
        "latest_available_periods": financials.latest_available_periods(),
    }
    if missing_fields:
        return {
            "ok": False,
            **metadata,
            "note": "required Futu field_id is missing",
            "missing_fields": missing_fields,
        }

    flags: dict[str, bool | None] = {}
    flags["positive_net_income"] = _positive(net_income)
    flags["positive_roa"] = _positive(latest_roa)
    flags["positive_operating_cash_flow"] = _positive(operating_cf)
    flags["cash_flow_gt_net_income"] = _greater(operating_cf, net_income)
    flags["lower_long_term_debt"] = (
        debt < prev_debt if prev_debt is not None else None
    )
    flags["higher_current_ratio"] = (
        current_ratio > prev_current_ratio
        if current_ratio is not None and prev_current_ratio is not None else None
    )
    flags["higher_gross_margin"] = (
        gross_margin > prev_gross_margin
        if gross_margin is not None and prev_gross_margin is not None else None
    )
    flags["has_valid_equity"] = bool(equity is not None and equity > 0)
    piotroski = sum(value is True for value in flags.values())
    piotroski_available = sum(value is not None for value in flags.values())

    return {
        "ok": True,
        **metadata,
        "periods": len(roe_values),
        "avg_roe_pct": safe_pct(sum(roe_values) / len(roe_values)) if roe_values else None,
        "min_roe_pct": safe_pct(min(roe_values)) if roe_values else None,
        "latest_roa_pct": safe_pct(latest_roa),
        "current_ratio": round(current_ratio, 4) if current_ratio is not None else None,
        "gross_margin_pct": safe_pct(gross_margin),
        "net_income": net_income,
        "operating_cash_flow": operating_cf,
        "piotroski_like_score": piotroski,
        "piotroski_like_available": piotroski_available,
        "piotroski_like_flags": flags,
    }


def supports_futu_refine(strategy) -> bool:
    """Only strategies that explicitly register an L2 refiner opt in to F10."""
    return callable(getattr(strategy, "refine_futu", None))


def run_futu_refine(candidates: list[dict[str, Any]], strategy,
                    config) -> list[dict[str, Any]]:
    if not supports_futu_refine(strategy):
        return candidates

    with _financial_lock:
        host = config.get("CONFIG", "FUTU_HOST", fallback="127.0.0.1")
        port = int(config.get("CONFIG", "FUTU_PORT", fallback=11111))
        cache: dict[tuple[str, str, int], tuple[FutuFinancialReport, ...]] = {}
        statement_names = getattr(
            strategy, "FUTU_L2_STATEMENT_TYPES", tuple(FINANCIAL_STATEMENT_TYPES),
        )
        refine_one = strategy.refine_futu

        ctx = ft.OpenQuoteContext(host=host, port=port)
        try:
            if not callable(getattr(ctx, "get_financials_statements", None)):
                raise RuntimeError(
                    "当前 futu-api 不支持 get_financials_statements；"
                    "请将 OpenD 和 futu-api 升级到 >= 10.6.6608"
                )
            total = len(candidates)
            for i, candidate in enumerate(candidates, 1):
                code = candidate["code"]
                print(f"[L2 {i}/{total}] {code} via Futu F10", file=sys.stderr)
                try:
                    statements = {
                        name: _fetch_financial_statement(ctx, cache, code, name)
                        for name in statement_names
                    }
                    financials = FutuFinancials(statements)
                    candidate["l2"] = refine_one(candidate, financials)
                except Exception as exc:  # noqa: BLE001
                    candidate["l2"] = {"ok": False, "source": "futu", "note": str(exc)}
            return candidates
        finally:
            ctx.close()


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
           refine: bool = False) -> dict[str, Any]:
    candidates = run_l1(strategy, market, config)
    l1_count = len(candidates)
    if snapshot:
        candidates = enrich_snapshot(candidates, strategy, config)
    l2_refined = 0
    l2_filtered = False
    l2_reject_reasons: dict[str, int] = {}
    if refine and supports_futu_refine(strategy):
        if candidates:
            run_futu_refine(candidates, strategy, config)
        l2_refined = len(candidates)
        # 显式启用 L2 且定义 l2_passes 的策略直接按其门槛过滤。
        passes = getattr(strategy, "l2_passes", None)
        if callable(passes):
            # 计数器只统计、不改变判据：通过与否一律以 l2_passes 为准，
            # l2_reject_reason 仅用于归因，两者不一致时以 l2_passes 为准。
            # 若策略没提供归因函数，落到 unclassified，保证计数能与
            # l2_refined − returned 对账。
            reason_of = getattr(strategy, "l2_reject_reason", None)
            kept = []
            for candidate in candidates:
                if passes(candidate):
                    kept.append(candidate)
                    continue
                reason = reason_of(candidate) if callable(reason_of) else None
                reason = reason or "unclassified"
                l2_reject_reasons[reason] = l2_reject_reasons.get(reason, 0) + 1
            candidates = kept
            l2_filtered = True
    return sanitize({
        "market": market,
        "strategy": strategy.NAME,
        "l1_count": l1_count,
        "snapshot_enriched": bool(snapshot),
        "l2_refined": l2_refined,
        "l2_filtered": l2_filtered,
        "l2_reject_reasons": l2_reject_reasons,
        "returned": len(candidates),
        "candidates": candidates,
    })


def parser(strategy) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=getattr(strategy, "DESCRIPTION", "Futu OpenD screener"),
    )
    ap.add_argument("--market", required=True, choices=list(MARKETS))
    ap.add_argument("--config", default="config_template.ini")
    ap.add_argument("--no-snapshot", action="store_true", help="只跑 get_stock_filter")
    ap.add_argument(
        "--refine", action="store_true",
        help="运行策略显式声明的 Futu F10 L2 精算",
    )
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
        refine=args.refine,
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
