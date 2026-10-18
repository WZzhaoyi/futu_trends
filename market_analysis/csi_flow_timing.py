#!/usr/bin/env python3
"""中证流通动量特征与月度阈值校准

z30、z60 动量特征，M1 口径（M1-LF-held-downside-exact-grid-strength-t1-defer-v2）的逐月阈值校准。

用法：
    python /absolute/path/csi_flow_timing.py fetch-bars --help
    python /absolute/path/csi_flow_timing.py calibrate --help
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import itertools
import json
import math
import os
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from live_runtime import (
    managed_futu_context,
    write_json_atomic,
)

try:
    import numpy as np
except ImportError:  # 校准可退回标准库实现
    np = None


STRATEGY = "m1"
STRATEGY_STATUS = "frozen_primary"
VERSION = "M1-LF-held-downside-exact-grid-strength-t1-defer-v2"
PUBLISH_SCHEMA_VERSION = 3
SEARCH_METHOD = "exact_frozen_grid"
WINDOW_MONTHS = 9
T1_SELL_MODE = "defer-next-open"   # 入场当日的卖出信号延到次一交易日首根开盘；写入发布文件
VOLATILITY_BARS = 32
CALIBRATION_COST_BPS = 5.0
CONSTRAINT_COST_BPS = 10.0
MIN_CONSTRAINT_RETURN = 0.0
MIN_ROUND_TRIPS = 4
MIN_EXPOSURE = 0.10
MAX_EXPOSURE = 0.90
MAX_CALIBRATION_EDGE_GAP_DAYS = 15
LIVE_MAINTENANCE_INTERVAL_SECONDS = 60
MARKET_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")

ENTRY_TIMES = {"10:30", "14:00"}
EXIT_TIMES = {"10:00", "10:30", "14:00"}

ENTRY_Z30_GRID = tuple(
    round(-0.50 + 0.25 * index, 2) for index in range(13)
)
ENTRY_Z60_GRID = tuple(
    round(-0.25 + 0.25 * index, 2) for index in range(11)
)
EXIT_Z30_GRID = tuple(
    round(-1.25 + 0.25 * index, 2) for index in range(7)
)
EXIT_Z60_GRID = tuple(
    round(-1.50 + 0.25 * index, 2) for index in range(8)
)
GRID_POINTS = (
    len(ENTRY_Z30_GRID)
    * len(ENTRY_Z60_GRID)
    * len(EXIT_Z30_GRID)
    * len(EXIT_Z60_GRID)
)


@dataclass(frozen=True)
class Bar:
    key: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    turnover: float = 0.0
    symbol: str = ""

    @property
    def trading_date(self) -> str:
        return self.key[:10]

    @property
    def clock(self) -> str:
        return self.key[11:16]


@dataclass(frozen=True)
class Feature:
    z30: float
    z60: float
    r30: float
    r60: float
    sigma: float


@dataclass(frozen=True)
class Threshold:
    entry_z30: float
    entry_z60: float
    exit_z30: float
    exit_z60: float
    month: str = ""
    source: str = field(default_factory=lambda: VERSION)


# Candidate and published threshold share the same four strategy parameters.
Candidate = Threshold


@dataclass(frozen=True)
class Evaluation:
    score: float
    strategy_return: float
    benchmark_return: float
    simple_excess: float
    relative_excess: float
    strategy_sortino: float
    held_market_downside_ratio: float
    max_drawdown: float
    median_quarter_excess: float
    worst_quarter_excess: float
    exposure: float
    buys: int
    sells: int


@dataclass(frozen=True)
class LiveRuntimePaths:
    root: Path
    bars_file: Path
    thresholds_dir: Path
    state_file: Path
    lock_file: Path

    @classmethod
    def from_argument(cls, raw_path: str) -> "LiveRuntimePaths":
        root = Path(raw_path).expanduser()
        if not root.is_absolute():
            raise ValueError("--runtime-dir 必须使用绝对路径")
        root = Path(os.path.abspath(root))
        return cls(
            root=root,
            bars_file=root / "calibration_bars.json",
            thresholds_dir=root / "thresholds",
            state_file=root / "state.json",
            lock_file=root / "live.lock",
        )

    def prepare(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.thresholds_dir.mkdir(parents=True, exist_ok=True)


def eprint(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bars(bars: list[Bar]) -> str:
    """Hash the exact timestamp/OHLC sequence consumed by calibration."""
    digest = hashlib.sha256()
    for bar in bars:
        row = (
            f"{bar.key}|{bar.open:.17g}|{bar.high:.17g}|"
            f"{bar.low:.17g}|{bar.close:.17g}\n"
        )
        digest.update(row.encode("utf-8"))
    return digest.hexdigest()


def normalize_symbol(value: str) -> str:
    text = str(value or "").strip().upper()
    if "." not in text:
        return text
    left, right = text.split(".", 1)
    if left in {"SH", "SZ", "HK", "US", "SG", "MY", "JP", "CC"}:
        return f"{right}.{left}"
    return text


def parse_timestamp(value: Any, shift_minutes: int = 0) -> str:
    if isinstance(value, (int, float)):
        parsed = datetime(1899, 12, 30) + timedelta(days=float(value))
    else:
        text = str(value).strip().replace("T", " ").replace("/", "-")
        if text.endswith("Z"):
            text = text[:-1]
        if not text:
            raise ValueError("空时间戳")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"无法解析时间戳: {value!r}") from exc
        if parsed.tzinfo is not None:
            parsed = parsed.replace(tzinfo=None)
    parsed += timedelta(minutes=shift_minutes)
    return parsed.strftime("%Y-%m-%d %H:%M")


def read_snapshot(path: Path, symbol: str = "") -> list[dict[str, Any]]:
    """读取 fetch-bars 写出的 15 分钟快照，返回按 K 线结束时间排序的行。

    快照须为不复权（autype=NONE），时间口径取文件中的 bar_time_convention（start 口径加 15 分钟）；
    给出 symbol 时须与快照标的一致。时间统一为 YYYY-MM-DD HH:MM，价格须为正，时间须严格递增。
    """
    with Path(path).open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict) or not isinstance(raw.get("bars"), list):
        raise ValueError(f"{path} 不是15分钟快照：缺少 bars")
    convention = raw.get("bar_time_convention")
    if convention not in ("end", "start") or raw.get("autype") != "NONE":
        raise ValueError(f"{path} 需要 bar_time_convention 为 end/start 且 autype=NONE")
    if symbol and normalize_symbol(str(raw.get("symbol") or "")) != normalize_symbol(symbol):
        raise ValueError(f"{path} 的标的是 {raw.get('symbol')}，不是 {symbol}")
    shift = 15 if convention == "start" else 0
    rows: list[dict[str, Any]] = []
    for row in raw["bars"]:
        try:
            parsed = {
                "time_key": parse_timestamp(row["time_key"], shift),
                **{name: float(row[name]) for name in ("open", "high", "low", "close")},
                "volume": float(row.get("volume") or 0),
                "turnover": float(row.get("turnover") or 0),
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"无法解析行情行: {row!r}") from exc
        if not all(math.isfinite(parsed[name]) and parsed[name] > 0 for name in ("open", "high", "low", "close")):
            raise ValueError(f"{parsed['time_key']} 存在非正数或非有限OHLC")
        if rows and parsed["time_key"] <= rows[-1]["time_key"]:
            raise ValueError(f"{path} 时间未严格递增: {parsed['time_key']}")
        rows.append(parsed)
    if not rows:
        raise ValueError(f"{path} 没有行情")
    return rows


def load_bars(path: Path, symbol: str = "") -> list[Bar]:
    rows = read_snapshot(path, symbol)
    return [Bar(key=row.pop("time_key"), symbol=symbol, **row) for row in rows]


def sample_std(values: list[float]) -> Optional[float]:
    if len(values) < 2:
        return None
    return statistics.stdev(values)


def build_features(bars: list[Bar]) -> list[Optional[Feature]]:
    log_returns: list[Optional[float]] = [None]
    for index in range(1, len(bars)):
        log_returns.append(math.log(bars[index].close / bars[index - 1].close))
    result: list[Optional[Feature]] = []
    for index, bar in enumerate(bars):
        if index < VOLATILITY_BARS + 1:
            result.append(None)
            continue
        sigma = sample_std(
            [
                value
                for value in log_returns[index - VOLATILITY_BARS : index]
                if value is not None
            ]
        )
        if sigma is None or sigma <= 0:
            result.append(None)
            continue
        r30 = bar.close / bars[index - 2].close - 1
        r60 = bar.close / bars[index - 4].close - 1
        result.append(
            Feature(
                z30=r30 / (sigma * math.sqrt(2)),
                z60=r60 / (sigma * 2),
                r30=r30,
                r60=r60,
                sigma=sigma,
            )
        )
    return result


def decide_at_close(
    position: int,
    t1_sellable: bool,
    clock: str,
    feature: Optional[Feature],
    threshold: Any,
) -> tuple[bool, bool, Optional[str], bool]:
    """Return raw signals, next-open action and same-day deferred sell."""
    if feature is None:
        return False, False, None, False
    buy_signal = bool(
        clock in ENTRY_TIMES
        and feature.z30 >= threshold.entry_z30
        and feature.z60 >= threshold.entry_z60
    )
    sell_signal = bool(
        clock in EXIT_TIMES
        and feature.z30 <= threshold.exit_z30
        and feature.z60 <= threshold.exit_z60
    )
    action: Optional[str] = None
    if position == 0 and buy_signal:
        action = "BUY"
    elif position == 1 and t1_sellable and sell_signal:
        action = "SELL"
    deferred_sell = bool(
        position == 1
        and not t1_sellable
        and sell_signal
    )
    return buy_signal, sell_signal, action, deferred_sell


def first_of_month(value: str) -> date:
    parsed = date.fromisoformat(value[:10])
    return parsed.replace(day=1)


def shift_months(value: date, months: int) -> date:
    absolute = value.year * 12 + value.month - 1 + months
    return date(absolute // 12, absolute % 12 + 1, 1)


def max_drawdown(values: list[float]) -> float:
    peak = 0.0
    result = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            result = min(result, value / peak - 1)
    return result


def quarter_index(day: str, boundaries: tuple[str, str, str]) -> int:
    if day < boundaries[0]:
        return 0
    if day < boundaries[1]:
        return 1
    if day < boundaries[2]:
        return 2
    return 3


def evaluate_candidate(
    bars: list[Bar],
    features: list[Optional[Feature]],
    candidate: Candidate,
    cost_bps: float,
) -> Evaluation:
    if not bars:
        raise ValueError("训练窗口没有行情")
    session_dates = sorted({bar.trading_date for bar in bars})
    session_index = {value: index for index, value in enumerate(session_dates)}
    window_start_month = first_of_month(bars[0].trading_date)
    cutoff = shift_months(first_of_month(bars[-1].trading_date), 1)
    span_months = (
        (cutoff.year - window_start_month.year) * 12
        + cutoff.month
        - window_start_month.month
    )
    offsets = tuple(
        max(1, round(span_months * fraction))
        for fraction in (0.25, 0.50, 0.75)
    )
    boundaries = tuple(
        shift_months(window_start_month, offset).isoformat()
        for offset in offsets
    )

    cash = 1.0
    units = 0.0
    state = 0
    pending: Optional[str] = None
    deferred_t1_sell = False
    entry_session: Optional[int] = None
    buys = sells = exposure_bars = 0
    cost = cost_bps / 10000
    first_open = bars[0].open
    equity_values: list[float] = []

    current_quarter = 0
    block_start_equity = 1.0
    block_start_benchmark = 1.0
    previous_equity = 1.0
    previous_benchmark = 1.0
    quarter_excess: list[float] = []
    previous_daily_equity = 1.0
    previous_daily_benchmark = 1.0
    strategy_daily_returns: list[float] = []
    held_downside_components: list[float] = []
    current_day_bars = 0
    current_day_held_bars = 0

    for index, bar in enumerate(bars):
        block = quarter_index(bar.trading_date, boundaries)
        if block != current_quarter:
            quarter_excess.append(
                (previous_equity / block_start_equity)
                / (previous_benchmark / block_start_benchmark)
                - 1
            )
            block_start_equity = previous_equity
            block_start_benchmark = previous_benchmark
            current_quarter = block

        session = session_index[bar.trading_date]
        deferred_ready = bool(
            deferred_t1_sell
            and state == 1
            and entry_session is not None
            and session > entry_session
        )
        execution_side = "SELL" if deferred_ready else pending
        if deferred_ready:
            deferred_t1_sell = False
        if execution_side == "SELL":
            cash = units * bar.open * (1 - cost)
            units = 0.0
            state = 0
            entry_session = None
            pending = None
            sells += 1
        elif execution_side == "BUY":
            units = cash * (1 - cost) / bar.open
            cash = 0.0
            state = 1
            entry_session = session
            buys += 1
            pending = None
        feature = features[index]
        if feature is not None and index < len(bars) - 1:
            t1_sellable = bool(
                state == 1
                and entry_session is not None
                and session > entry_session
            )
            _, _, action, defer_sell = decide_at_close(
                state,
                t1_sellable,
                bar.clock,
                feature,
                candidate,
            )
            if action is not None:
                pending = action
            elif defer_sell:
                deferred_t1_sell = True

        if units > 0:
            exposure_bars += 1
            current_day_held_bars += 1
        current_day_bars += 1
        previous_equity = cash + units * bar.close
        previous_benchmark = bar.close / first_open
        equity_values.append(previous_equity)
        day_end = (
            index == len(bars) - 1
            or bars[index + 1].trading_date != bar.trading_date
        )
        if day_end:
            strategy_daily = previous_equity / previous_daily_equity - 1
            benchmark_daily = previous_benchmark / previous_daily_benchmark - 1
            strategy_daily_returns.append(strategy_daily)
            day_exposure = current_day_held_bars / current_day_bars
            held_downside_components.append(
                day_exposure * min(benchmark_daily, 0.0)
            )
            previous_daily_equity = previous_equity
            previous_daily_benchmark = previous_benchmark
            current_day_bars = 0
            current_day_held_bars = 0

    quarter_excess.append(
        (previous_equity / block_start_equity)
        / (previous_benchmark / block_start_benchmark)
        - 1
    )
    while len(quarter_excess) < 4:
        quarter_excess.insert(0, 0.0)

    strategy_return = previous_equity - 1
    benchmark_return = previous_benchmark - 1
    strategy_downside = math.sqrt(
        statistics.mean(min(value, 0.0) ** 2 for value in strategy_daily_returns)
    )
    strategy_sortino = (
        statistics.mean(strategy_daily_returns)
        / strategy_downside
        * math.sqrt(252)
        if strategy_downside > 1e-12
        else -math.inf
    )
    held_downside = math.sqrt(
        statistics.mean(value**2 for value in held_downside_components)
    )
    held_ratio = (
        statistics.mean(strategy_daily_returns)
        / held_downside
        * math.sqrt(252)
        if held_downside > 1e-12
        else -math.inf
    )
    return Evaluation(
        score=held_ratio,
        strategy_return=strategy_return,
        benchmark_return=benchmark_return,
        simple_excess=strategy_return - benchmark_return,
        relative_excess=previous_equity / previous_benchmark - 1,
        strategy_sortino=strategy_sortino,
        held_market_downside_ratio=held_ratio,
        max_drawdown=max_drawdown(equity_values),
        median_quarter_excess=statistics.median(quarter_excess),
        worst_quarter_excess=min(quarter_excess),
        exposure=exposure_bars / len(bars),
        buys=buys,
        sells=sells,
    )


def choose_global_optimum(
    rows: list[tuple[Candidate, Evaluation]],
) -> tuple[Candidate, Evaluation]:
    """Return the exact objective maximum with no temporal anchor.

    All calibration constraints are hard. If multiple candidates have exactly
    the same floating-point score, prefer the smallest L1 threshold norm and
    then the lexicographically smallest tuple. The tie-break is deterministic,
    independent of prior months, and never admits a lower primary score.
    """
    eligible = [
        row
        for row in rows
        if min(row[1].buys, row[1].sells) >= MIN_ROUND_TRIPS
        and MIN_EXPOSURE <= row[1].exposure <= MAX_EXPOSURE
        and math.isfinite(row[1].score)
    ]
    if not eligible:
        raise RuntimeError(
            "完整阈值网格中没有同时满足成本、交易次数、暴露和有限目标的候选"
        )
    best_score = max(row[1].score for row in eligible)
    exact_ties = [row for row in eligible if row[1].score == best_score]
    return min(
        exact_ties,
        key=lambda row: (
            abs(row[0].entry_z30)
            + abs(row[0].entry_z60)
            + abs(row[0].exit_z30)
            + abs(row[0].exit_z60),
            row[0].entry_z30,
            row[0].entry_z60,
            row[0].exit_z30,
            row[0].exit_z60,
        ),
    )


def optimize_exhaustive(
    bars: list[Bar],
    features: list[Optional[Feature]],
    cache: dict[Candidate, Evaluation],
    constraint_cache: dict[Candidate, Evaluation],
) -> tuple[Candidate, Evaluation]:
    """Standard-library fallback for the exact 8,008-point grid."""
    rows: list[tuple[Candidate, Evaluation]] = []
    for values in itertools.product(
        ENTRY_Z30_GRID,
        ENTRY_Z60_GRID,
        EXIT_Z30_GRID,
        EXIT_Z60_GRID,
    ):
        candidate = Candidate(*values)
        evaluation = cache.get(candidate)
        if evaluation is None:
            evaluation = evaluate_candidate(
                bars,
                features,
                candidate,
                CALIBRATION_COST_BPS,
            )
            cache[candidate] = evaluation
        constraint = constraint_cache.get(candidate)
        if constraint is None:
            constraint = evaluate_candidate(
                bars,
                features,
                candidate,
                CONSTRAINT_COST_BPS,
            )
            constraint_cache[candidate] = constraint
        if constraint.strategy_return >= MIN_CONSTRAINT_RETURN:
            rows.append((candidate, evaluation))
    return choose_global_optimum(rows)


def optimize_exhaustive_vectorized(
    bars: list[Bar],
    features: list[Optional[Feature]],
) -> tuple[Candidate, Evaluation, int, Evaluation]:
    """Evaluate all 8,008 candidates exactly with vectorized state arrays.

    Candidate states are independent; NumPy only evaluates them in parallel.
    The path, next-bar execution, T+1 rule, objective and tie-break are the
    same as ``evaluate_candidate``. The chosen candidate is re-evaluated by
    that scalar implementation before publication.
    """
    if np is None:
        cache: dict[Candidate, Evaluation] = {}
        constraint_cache: dict[Candidate, Evaluation] = {}
        candidate, evaluation = optimize_exhaustive(
            bars, features, cache, constraint_cache
        )
        return (
            candidate,
            evaluation,
            len(cache),
            constraint_cache[candidate],
        )

    candidates = [
        Candidate(*values)
        for values in itertools.product(
            ENTRY_Z30_GRID,
            ENTRY_Z60_GRID,
            EXIT_Z30_GRID,
            EXIT_Z60_GRID,
        )
    ]
    count = len(candidates)
    entry_z30 = np.array(
        [candidate.entry_z30 for candidate in candidates], dtype=float
    )
    entry_z60 = np.array(
        [candidate.entry_z60 for candidate in candidates], dtype=float
    )
    exit_z30 = np.array(
        [candidate.exit_z30 for candidate in candidates], dtype=float
    )
    exit_z60 = np.array(
        [candidate.exit_z60 for candidate in candidates], dtype=float
    )
    costs = np.array(
        [CALIBRATION_COST_BPS, CONSTRAINT_COST_BPS], dtype=float
    ) / 10000
    cash = np.ones((2, count), dtype=float)
    units = np.zeros((2, count), dtype=float)
    holding = np.zeros(count, dtype=bool)
    pending = np.zeros(count, dtype=np.int8)
    deferred_t1_sell = np.zeros(count, dtype=bool)
    entry_session = np.full(count, -1, dtype=np.int32)
    buys = np.zeros(count, dtype=np.int16)
    sells = np.zeros(count, dtype=np.int16)
    exposure_bars = np.zeros(count, dtype=np.int32)
    day_held_bars = np.zeros(count, dtype=np.int16)
    day_bar_count = 0
    previous_daily_equity = np.ones((2, count), dtype=float)
    previous_daily_benchmark = 1.0
    daily_return_sum = np.zeros(count, dtype=float)
    held_downside_square_sum = np.zeros(count, dtype=float)
    day_count = 0
    last_equity = np.ones((2, count), dtype=float)

    session_dates = sorted({bar.trading_date for bar in bars})
    session_lookup = {
        trading_date: index
        for index, trading_date in enumerate(session_dates)
    }
    sessions = np.array(
        [session_lookup[bar.trading_date] for bar in bars], dtype=np.int32
    )
    first_open = bars[0].open

    for index, bar in enumerate(bars):
        deferred_open = (
            deferred_t1_sell
            & holding
            & (sessions[index] > entry_session)
        )
        if np.any(deferred_open):
            cash[:, deferred_open] = (
                units[:, deferred_open]
                * bar.open
                * (1 - costs[:, None])
            )
            units[:, deferred_open] = 0.0
            holding[deferred_open] = False
            entry_session[deferred_open] = -1
            deferred_t1_sell[deferred_open] = False
            pending[deferred_open] = 0
            sells[deferred_open] += 1

        buy_pending = pending == 1
        if np.any(buy_pending):
            units[:, buy_pending] = (
                cash[:, buy_pending]
                * (1 - costs[:, None])
                / bar.open
            )
            cash[:, buy_pending] = 0.0
            holding[buy_pending] = True
            entry_session[buy_pending] = sessions[index]
            pending[buy_pending] = 0
            buys[buy_pending] += 1

        sell_pending = pending == 2
        if np.any(sell_pending):
            cash[:, sell_pending] = (
                units[:, sell_pending]
                * bar.open
                * (1 - costs[:, None])
            )
            units[:, sell_pending] = 0.0
            holding[sell_pending] = False
            entry_session[sell_pending] = -1
            pending[sell_pending] = 0
            sells[sell_pending] += 1

        feature = features[index]
        if feature is not None and index < len(bars) - 1:
            no_pending = pending == 0
            if bar.clock in ENTRY_TIMES:
                buy_signal = (
                    (~holding)
                    & no_pending
                    & (feature.z30 >= entry_z30)
                    & (feature.z60 >= entry_z60)
                )
                pending[buy_signal] = 1
            if bar.clock in EXIT_TIMES:
                sell_score = (
                    (feature.z30 <= exit_z30)
                    & (feature.z60 <= exit_z60)
                )
                deferred_signal = (
                    holding
                    & no_pending
                    & (sessions[index] == entry_session)
                    & sell_score
                )
                deferred_t1_sell[deferred_signal] = True
                sell_signal = (
                    holding
                    & no_pending
                    & (sessions[index] > entry_session)
                    & sell_score
                )
                pending[sell_signal] = 2

        equity = cash + units * bar.close
        exposure_bars[holding] += 1
        day_held_bars[holding] += 1
        day_bar_count += 1
        last_equity = equity
        day_end = (
            index == len(bars) - 1
            or bars[index + 1].trading_date != bar.trading_date
        )
        if day_end:
            strategy_daily = equity / previous_daily_equity - 1
            benchmark = bar.close / first_open
            benchmark_daily = benchmark / previous_daily_benchmark - 1
            day_exposure = day_held_bars / day_bar_count
            daily_return_sum += strategy_daily[0]
            held_downside_square_sum += (
                day_exposure * min(benchmark_daily, 0.0)
            ) ** 2
            previous_daily_equity = equity.copy()
            previous_daily_benchmark = benchmark
            day_held_bars.fill(0)
            day_bar_count = 0
            day_count += 1

    held_downside = np.sqrt(held_downside_square_sum / day_count)
    scores = np.full(count, -np.inf)
    valid = held_downside > 1e-12
    scores[valid] = (
        (daily_return_sum[valid] / day_count)
        / held_downside[valid]
        * math.sqrt(252)
    )
    stress_return = last_equity[1] - 1
    exposure = exposure_bars / len(bars)
    eligible = (
        (np.minimum(buys, sells) >= MIN_ROUND_TRIPS)
        & (exposure >= MIN_EXPOSURE)
        & (exposure <= MAX_EXPOSURE)
        & np.isfinite(scores)
        & (stress_return >= MIN_CONSTRAINT_RETURN)
    )
    indexes = np.flatnonzero(eligible)
    if not len(indexes):
        raise RuntimeError(
            "完整阈值网格中没有同时满足成本、交易次数、暴露和有限目标的候选"
        )
    best_score = np.max(scores[indexes])
    ties = [
        int(index)
        for index in indexes
        if scores[index] == best_score
    ]
    chosen_index = min(
        ties,
        key=lambda index: (
            abs(candidates[index].entry_z30)
            + abs(candidates[index].entry_z60)
            + abs(candidates[index].exit_z30)
            + abs(candidates[index].exit_z60),
            candidates[index].entry_z30,
            candidates[index].entry_z60,
            candidates[index].exit_z30,
            candidates[index].exit_z60,
        ),
    )
    candidate = candidates[chosen_index]
    evaluation = evaluate_candidate(
        bars, features, candidate, CALIBRATION_COST_BPS
    )
    constraint = evaluate_candidate(
        bars, features, candidate, CONSTRAINT_COST_BPS
    )
    return candidate, evaluation, count, constraint


def calibrate_month(
    bars: list[Bar],
    features: list[Optional[Feature]],
    cutoff: date,
    window_months: int = WINDOW_MONTHS,
) -> tuple[Candidate, Evaluation, int, Evaluation]:
    window_start = shift_months(cutoff, -window_months).isoformat()
    cutoff_text = cutoff.isoformat()
    indices = [
        index
        for index, bar in enumerate(bars)
        if window_start <= bar.trading_date < cutoff_text
    ]
    if not indices:
        raise ValueError(f"{cutoff_text} 前的校准窗口没有行情")
    train_bars = [bars[index] for index in indices]
    train_features = [features[index] for index in indices]
    start_gap = (
        date.fromisoformat(train_bars[0].trading_date)
        - date.fromisoformat(window_start)
    ).days
    if start_gap > MAX_CALIBRATION_EDGE_GAP_DAYS:
        raise ValueError(
            f"{cutoff_text} 训练窗口不完整，首根行情为 "
            f"{train_bars[0].trading_date}"
        )
    end_gap = (
        cutoff - date.fromisoformat(train_bars[-1].trading_date)
    ).days
    if end_gap > MAX_CALIBRATION_EDGE_GAP_DAYS:
        raise ValueError(
            f"{cutoff_text} 训练窗口尾部数据陈旧，最后行情为 "
            f"{train_bars[-1].trading_date}"
        )

    return optimize_exhaustive_vectorized(train_bars, train_features)


def load_threshold(path: Path) -> Threshold:
    """读取 calibrate 发布的单月阈值文件。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    published = raw.get("threshold") if isinstance(raw, dict) else None
    if not isinstance(published, dict):
        raise ValueError(f"{path} 不是 calibrate 发布的月度阈值")
    month = str(published.get("month") or raw.get("month") or "")[:7]
    if not month:
        raise ValueError(f"{path} 缺少阈值月份")
    try:
        values = {key: float(published[key]) for key in ("entry_z30", "entry_z60", "exit_z30", "exit_z60")}
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{path} 阈值数值无效") from exc
    return Threshold(month=month, source=str(published.get("source") or raw.get("version", VERSION)), **values)


def load_live_threshold(
    path: Path,
    symbol: str,
    window_months: int = WINDOW_MONTHS,
) -> Threshold:
    """Load and validate an audited monthly publication for live inference."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("threshold"), dict):
        raise ValueError(
            "live 阈值必须是 calibrate 发布的月度JSON，"
            "不能使用研究回测阈值表"
        )
    if raw.get("schema_version") != PUBLISH_SCHEMA_VERSION:
        raise ValueError(
            f"阈值发布 schema_version 必须为 {PUBLISH_SCHEMA_VERSION}"
        )
    if raw.get("publication_kind") != "monthly_threshold":
        raise ValueError("阈值文件 publication_kind 不是 monthly_threshold")
    if raw.get("version") != VERSION:
        raise ValueError(
            f"阈值版本 {raw.get('version')!r} 与脚本 {VERSION!r} 不一致"
        )
    if raw.get("strategy") != STRATEGY:
        raise ValueError(
            f"阈值策略 {raw.get('strategy')!r} 与脚本 "
            f"{STRATEGY!r} 不一致"
        )
    if raw.get("strategy_status") != STRATEGY_STATUS:
        raise ValueError(
            f"阈值策略状态 {raw.get('strategy_status')!r} 与脚本 "
            f"{STRATEGY_STATUS!r} 不一致"
        )
    if int(raw.get("window_months", 0)) != window_months:
        raise ValueError(
            f"阈值校准窗口不是本次指定的 {window_months} 个月"
        )
    if raw.get("t1_sell_mode") != T1_SELL_MODE:
        raise ValueError(f"阈值T+1退出模式不是 {T1_SELL_MODE}")
    if int(raw.get("grid_points", 0)) != GRID_POINTS:
        raise ValueError(f"阈值网格点数不是冻结的 {GRID_POINTS}")
    if raw.get("search_method") != SEARCH_METHOD:
        raise ValueError("阈值文件不是完整精确网格搜索结果")
    if int(raw.get("search_evaluations", 0)) != GRID_POINTS:
        raise ValueError(
            f"阈值文件搜索数不是完整网格 {GRID_POINTS}"
        )
    if float(raw.get("constraint_strategy_return", -math.inf)) < (
        MIN_CONSTRAINT_RETURN
    ):
        raise ValueError("阈值文件未通过10bp训练收益约束")
    training = raw.get("training_evaluation")
    if not isinstance(training, dict):
        raise ValueError("阈值文件缺少 training_evaluation")
    if min(int(training.get("buys", 0)), int(training.get("sells", 0))) < (
        MIN_ROUND_TRIPS
    ):
        raise ValueError("阈值文件未通过最少往返交易约束")
    exposure = float(training.get("exposure", math.nan))
    score = float(training.get("score", math.nan))
    if not MIN_EXPOSURE <= exposure <= MAX_EXPOSURE:
        raise ValueError("阈值文件未通过训练暴露约束")
    if not math.isfinite(score):
        raise ValueError("阈值文件训练目标不是有限数")
    published_symbol = str(raw.get("symbol", ""))
    if normalize_symbol(published_symbol) != normalize_symbol(symbol):
        raise ValueError(
            f"阈值标的 {published_symbol!r} 与 live 标的 {symbol!r} 不一致"
        )
    month = str(raw.get("month", ""))[:7]
    available_from = str(raw.get("available_from", ""))
    window_end = str(raw.get("window_end", ""))
    if not month or available_from[:7] != month:
        raise ValueError("阈值月份与 available_from 不一致")
    if not window_end or window_end >= available_from:
        raise ValueError("阈值训练窗口没有严格截止于生效日以前")
    data_audit = raw.get("data_audit")
    if not isinstance(data_audit, dict):
        raise ValueError("阈值文件缺少 data_audit")
    required_hashes = (
        str(data_audit.get("source_file_sha256", "")),
        str(data_audit.get("training_bars_sha256", "")),
        str(raw.get("script_sha256", "")),
    )
    if not all(len(value) == 64 for value in required_hashes):
        raise ValueError("阈值文件缺少有效SHA-256审计字段")
    current_script_sha256 = sha256_file(Path(__file__).resolve())
    if raw.get("script_sha256") != current_script_sha256:
        raise ValueError(
            "阈值文件由不同脚本内容生成；请用当前脚本重新校准发布"
        )

    threshold = raw["threshold"]
    grid_checks = (
        ("entry_z30", ENTRY_Z30_GRID),
        ("entry_z60", ENTRY_Z60_GRID),
        ("exit_z30", EXIT_Z30_GRID),
        ("exit_z60", EXIT_Z60_GRID),
    )
    for key, grid in grid_checks:
        if float(threshold.get(key, math.nan)) not in grid:
            raise ValueError(f"阈值 {key} 不在冻结网格内")
    return load_threshold(path)


class ThresholdDirectoryProvider:
    """Load audited monthly publications lazily so live need not restart."""

    def __init__(
        self,
        directory: Path,
        symbol: str,
        window_months: int = WINDOW_MONTHS,
    ) -> None:
        if not directory.is_dir():
            raise ValueError(f"阈值目录不存在: {directory}")
        self.directory = directory
        self.symbol = symbol
        self.window_months = window_months
        self.cache: dict[str, tuple[tuple[int, int], Threshold]] = {}

    def for_date(self, value: str) -> Threshold:
        month = value[:7]
        path = self.directory / f"threshold_{month}.json"
        if not path.is_file():
            raise ValueError(f"缺少 {path}；应由 live 自动发布当月阈值")
        stat = path.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
        cached = self.cache.get(month)
        if cached is None or cached[0] != signature:
            threshold = load_live_threshold(path, symbol=self.symbol, window_months=self.window_months)
            if threshold.month != month:
                raise ValueError(f"{path} 发布月份不是文件名声明的 {month}")
            cached = self.cache[month] = (signature, threshold)
        return cached[1]


def resolve_live_connection(config_path: Optional[str]) -> tuple[str, int]:
    if not config_path:
        return "127.0.0.1", 11111
    config = configparser.ConfigParser()
    path = Path(config_path).resolve()
    if not config.read(path, encoding="utf-8"):
        raise ValueError(f"配置文件不存在或不可读: {path}")
    return (
        config.get("CONFIG", "FUTU_HOST", fallback="127.0.0.1"),
        config.getint("CONFIG", "FUTU_PORT", fallback=11111),
    )


def pull_history(context, symbol: str, start: str, end: str, autype: Any, KLType: Any, RET_OK: Any) -> list[dict[str, Any]]:
    """按页拉取 15 分钟历史 K 线，返回 time_key 与 OHLC、成交量、成交额。"""
    rows: list[dict[str, Any]] = []
    page_key = None
    while True:
        ret, data, page_key = context.request_history_kline(
            symbol, start=start, end=end, ktype=KLType.K_15M, autype=autype,
            max_count=1000, page_req_key=page_key,
        )
        if ret != RET_OK:
            raise RuntimeError(f"{symbol} 获取历史15分钟K线失败: {data}")
        records = data.to_dict("records") if hasattr(data, "to_dict") else list(data)
        rows.extend(
            {"time_key": str(row.get("time_key", "")),
             **{name: float(row.get(name) or 0) for name in ("open", "high", "low", "close", "volume", "turnover")}}
            for row in records
        )
        if page_key is None:
            return rows
        time.sleep(0.4)


def fetch_calibration_bars(
    *,
    symbol: str,
    as_of: str,
    output: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    config: Optional[str] = None,
    futu_time_convention: str = "end",
    window_months: int = WINDOW_MONTHS,
) -> dict[str, Any]:
    """Refresh the rolling calibration input from Futu historical K-lines."""
    try:
        from futu import AuType, KLType, OpenQuoteContext, RET_OK
    except ImportError as exc:
        raise RuntimeError(
            "fetch-bars 需要 futu-api；安装命令: pip install futu-api"
        ) from exc

    cutoff = first_of_month(as_of)
    start = start or shift_months(
        cutoff, -(window_months + 1)
    ).isoformat()
    end = end or cutoff.fromordinal(cutoff.toordinal() - 1).isoformat()
    if start > end:
        raise ValueError(f"历史行情日期范围无效: {start} > {end}")

    host, port = resolve_live_connection(config)
    with managed_futu_context(OpenQuoteContext, host=host, port=port) as context:
        rows = pull_history(context, symbol, start, end, AuType.NONE, KLType, RET_OK)

    by_key = {row["time_key"]: row for row in rows if row["time_key"]}
    bars = [by_key[key] for key in sorted(by_key)]
    if not bars:
        raise ValueError(f"{symbol} 在 {start} 至 {end} 没有15分钟K线")
    payload = {
        "schema_version": 1,
        "source": "futu.request_history_kline",
        "symbol": symbol,
        "autype": "NONE",
        "fetched_at": datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "start": start,
        "end": end,
        "window_months": window_months,
        "bar_time_convention": futu_time_convention,
        "bars": bars,
    }
    output_path = Path(output).resolve()
    write_json_atomic(output_path, payload)
    return {
        "output": str(output_path),
        "symbol": symbol,
        "start": start,
        "end": end,
        "bars": len(bars),
        "first_bar": bars[0]["time_key"],
        "last_bar": bars[-1]["time_key"],
        "window_months": window_months,
    }


def run_fetch_bars(args: argparse.Namespace) -> int:
    result = fetch_calibration_bars(
        symbol=args.symbol,
        as_of=args.as_of,
        output=args.output,
        start=args.start,
        end=args.end,
        config=args.config,
        futu_time_convention=args.futu_time_convention,
        window_months=int(getattr(args, "window_months", WINDOW_MONTHS)),
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


def publish_calibration(
    *,
    bars_file: str,
    symbol: str,
    as_of: str,
    output: str,
    window_months: int = WINDOW_MONTHS,
) -> dict[str, Any]:
    bars_path = Path(bars_file).resolve()
    bars = load_bars(bars_path, symbol=symbol)
    features = build_features(bars)
    cutoff = first_of_month(as_of)
    window_start = shift_months(cutoff, -window_months).isoformat()
    cutoff_text = cutoff.isoformat()
    training_bars = [
        bar
        for bar in bars
        if window_start <= bar.trading_date < cutoff_text
    ]
    candidate, evaluation, count, constraint = calibrate_month(
        bars, features, cutoff, window_months
    )
    threshold = replace(candidate, month=cutoff.strftime("%Y-%m"))
    payload = {
        "schema_version": PUBLISH_SCHEMA_VERSION,
        "publication_kind": "monthly_threshold",
        "strategy": STRATEGY,
        "strategy_status": STRATEGY_STATUS,
        "version": VERSION,
        "script_sha256": sha256_file(Path(__file__).resolve()),
        "published_at": datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "symbol": normalize_symbol(symbol),
        "month": cutoff.strftime("%Y-%m"),
        "available_from": cutoff.isoformat(),
        "window_start": window_start,
        "window_end": cutoff.fromordinal(cutoff.toordinal() - 1).isoformat(),
        "search_method": SEARCH_METHOD,
        "window_months": window_months,
        "t1_sell_mode": T1_SELL_MODE,
        "grid_points": GRID_POINTS,
        "threshold": asdict(threshold),
        "training_evaluation": asdict(evaluation),
        "constraint_cost_bps": CONSTRAINT_COST_BPS,
        "constraint_strategy_return": constraint.strategy_return,
        "search_evaluations": count,
        "data_audit": {
            "bars_file": str(bars_path),
            "source_file_sha256": sha256_file(bars_path),
            "training_bars_sha256": sha256_bars(training_bars),
            "training_bar_count": len(training_bars),
            "first_training_bar": training_bars[0].key,
            "last_training_bar": training_bars[-1].key,
        },
    }
    write_json_atomic(Path(output).resolve(), payload)
    return payload


def run_calibrate(args: argparse.Namespace) -> int:
    payload = publish_calibration(
        bars_file=args.bars_file,
        symbol=args.symbol,
        as_of=args.as_of,
        output=args.output,
        window_months=int(getattr(args, "window_months", WINDOW_MONTHS)),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def current_market_month(now: Optional[datetime] = None) -> date:
    current = now or datetime.now(MARKET_TIMEZONE)
    if current.tzinfo is None:
        current = current.replace(tzinfo=MARKET_TIMEZONE)
    else:
        current = current.astimezone(MARKET_TIMEZONE)
    return current.date().replace(day=1)


def ensure_live_threshold(
    args: argparse.Namespace,
    runtime: LiveRuntimePaths,
    now: Optional[datetime] = None,
) -> tuple[str, bool]:
    """Ensure the current month's audited threshold exists before inference."""
    runtime.prepare()
    window_months = int(getattr(args, "window_months", WINDOW_MONTHS))
    cutoff = current_market_month(now)
    month = cutoff.strftime("%Y-%m")
    publication = runtime.thresholds_dir / f"threshold_{month}.json"
    if publication.is_file():
        try:
            if load_live_threshold(publication, args.symbol, window_months).month != month:
                raise ValueError(f"发布月份不是 {month}")
            return month, False
        except (
            ValueError,
            OSError,
            json.JSONDecodeError,
        ) as exc:
            eprint(f"{publication} 审计失效，重新生成: {exc}")

    cutoff_text = cutoff.isoformat()
    fetch_calibration_bars(
        symbol=args.symbol,
        as_of=cutoff_text,
        output=str(runtime.bars_file),
        config=args.config,
        futu_time_convention=args.futu_time_convention,
        window_months=window_months,
    )
    publish_calibration(
        bars_file=str(runtime.bars_file),
        symbol=args.symbol,
        as_of=cutoff_text,
        output=str(publication),
        window_months=window_months,
    )
    if load_live_threshold(publication, args.symbol, window_months).month != month:
        raise ValueError(f"{publication} 发布月份不是 {month}")
    return month, True


def integer_at_least(minimum: int) -> Callable[[str], int]:
    def parse(value: str) -> int:
        try:
            parsed = int(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError("必须是整数") from exc
        if parsed < minimum:
            raise argparse.ArgumentTypeError(f"必须不小于 {minimum}")
        return parsed

    return parse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=f"{VERSION} 动量阈值校准"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def option(*flags: str, **values: Any):
        return flags, values

    window = option(
        "--window-months",
        type=integer_at_least(1),
        default=WINDOW_MONTHS,
        help=f"M1阈值滚动校准月数（默认 {WINDOW_MONTHS}）",
    )
    bar_arguments = [
        option("--bars-file", required=True, help="fetch-bars 写出的15分钟快照 JSON"),
        option("--symbol", default="000902.SH"),
    ]
    commands = {
        "fetch-bars": (
            "从Futu刷新月度校准所需的15分钟历史K线", run_fetch_bars,
            [
                option("--symbol", default="SH.000902"),
                window,
                option("--as-of", required=True, help="目标月份首日"),
                option("--start", help="可选覆盖历史起始日"),
                option("--end", help="可选覆盖历史截止日"),
                option("--output", required=True),
                option("--config", help="项目config.ini"),
                option(
                    "--futu-time-convention",
                    choices=("end", "start"),
                    default="end",
                    help="记录Futu time_key口径，供后续calibrate使用",
                ),
            ],
        ),
        "calibrate": (
            "仅使用目标月份以前数据生成当月阈值", run_calibrate,
            bar_arguments
            + [
                window,
                option("--as-of", required=True),
                option("--output", required=True, help="原子发布的月度阈值JSON路径"),
            ],
        ),
    }
    for name, (help_text, command, arguments) in commands.items():
        child = subparsers.add_parser(name, help=help_text)
        for flags, options in arguments:
            child.add_argument(*flags, **options)
        child.set_defaults(func=command)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        eprint(f"错误: {exc}")
        raise SystemExit(2)
