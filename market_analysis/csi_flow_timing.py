#!/usr/bin/env python3
"""中证流通择时信号。

策略版本：M1-LF-held-downside-exact-grid-strength-t1-defer-v2

用法：
    python /absolute/path/csi_flow_timing.py backtest --help
    python /absolute/path/csi_flow_timing.py fetch-bars --help
    python /absolute/path/csi_flow_timing.py calibrate --help
    python /absolute/path/csi_flow_timing.py live --help

实时示例：
    python /absolute/path/csi_flow_timing.py live \
        --runtime-dir /absolute/path/runtime --window-months 9 --n 9
"""

from __future__ import annotations

import argparse
import configparser
import csv
import hashlib
import itertools
import json
import math
import os
import queue
import signal
import statistics
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional

try:
    import numpy as np
except ImportError:  # live inference itself does not require NumPy
    np = None


STRATEGY = "m1"
STRATEGY_STATUS = "frozen_primary"
VERSION = "M1-LF-held-downside-exact-grid-strength-t1-defer-v2"
LEGACY_STATE_VERSIONS = {"M1-LF-held-downside-exact-grid-strength-v1"}
PUBLISH_SCHEMA_VERSION = 3
SEARCH_METHOD = "exact_frozen_grid"
WINDOW_MONTHS = 9
STRENGTH_N = 9
STRENGTH_FORMULA = "15m_log_price_regression_return"
NOTIFICATION_MODES = ("position-aware", "position-independent")
T1_SELL_MODES = ("defer-next-open", "ignore-same-day")
T1_SELL_MODE = "defer-next-open"
STRENGTH_SYMBOLS = (
    "SH.510500",
    "SH.510050",
    "SH.510300",
    "SH.512100",
)
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
ALL_DECISION_TIMES = ENTRY_TIMES | EXIT_TIMES

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
class Action:
    side: str
    signal_key: str
    execution_key: str
    execution_price: float
    calibration_month: str


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


@contextmanager
def runtime_file_lock(path: Path) -> Iterator[None]:
    """Hold one OS-released lock for the lifetime of a live instance."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            raise RuntimeError(
                f"已有 live 实例占用运行目录: {path.parent}"
            ) from exc
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


RuntimeFileLock = runtime_file_lock


def eprint(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


@contextmanager
def managed_futu_context(
    factory: Callable[..., Any],
    *,
    host: str,
    port: int,
) -> Iterator[Any]:
    """Close one Futu context on every Python exit path without masking errors."""
    context = factory(host=host, port=port)

    def close() -> None:
        try:
            context.close()
        except Exception as exc:
            eprint(f"警告: 关闭 Futu context 失败: {exc}")

    try:
        yield context
    finally:
        close()


@contextmanager
def graceful_stop_event() -> Iterator[threading.Event]:
    """Convert SIGINT/SIGTERM into a cooperative stop so contexts can close."""
    stopped = threading.Event()

    def stop_handler(_signum: int, _frame: Any) -> None:
        stopped.set()

    old_sigint = signal.signal(signal.SIGINT, stop_handler)
    old_sigterm = signal.signal(signal.SIGTERM, stop_handler)
    try:
        yield stopped
    finally:
        signal.signal(signal.SIGINT, old_sigint)
        signal.signal(signal.SIGTERM, old_sigterm)


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


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def normalize_symbol(value: str) -> str:
    text = str(value or "").strip().upper()
    if "." not in text:
        return text
    left, right = text.split(".", 1)
    if left in {"SH", "SZ", "HK", "US", "SG", "MY", "JP", "CC"}:
        return f"{right}.{left}"
    return text


def canonical_strength_symbol(value: str) -> str:
    normalized = normalize_symbol(value)
    for symbol in STRENGTH_SYMBOLS:
        if normalize_symbol(symbol) == normalized:
            return symbol
    raise ValueError(
        f"不支持的强度标的 {value!r}；需要 {', '.join(STRENGTH_SYMBOLS)}"
    )


def parse_symbol_path(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("参数必须为 SYMBOL=/absolute/path")
    raw_symbol, raw_path = value.split("=", 1)
    try:
        symbol = canonical_strength_symbol(raw_symbol)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return symbol, Path(raw_path).expanduser().resolve()


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


def pick(record: dict[str, Any], *names: str) -> Any:
    lowered = {str(key).strip().lower(): value for key, value in record.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


def read_records(path: Path) -> Any:
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def unwrap_records(raw: Any, keys: Iterable[str]) -> list[Any]:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for key in keys:
            value = raw.get(key)
            if isinstance(value, list):
                return value
    raise ValueError("文件不是受支持的记录数组格式")


def load_bars(
    path: Path,
    symbol: str = "",
    time_convention: str = "end",
) -> list[Bar]:
    rows = unwrap_records(read_records(path), ("data", "bars", "records"))
    wanted = normalize_symbol(symbol) if symbol else ""
    shift = 15 if time_convention == "start" else 0
    by_key: dict[str, Bar] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_symbol = str(pick(row, "symbol", "code", "ticker") or "")
        if wanted and row_symbol and normalize_symbol(row_symbol) != wanted:
            continue
        raw_time = pick(row, "key", "time", "time_key", "timestamp", "datetime")
        if raw_time in (None, ""):
            continue
        try:
            bar = Bar(
                key=parse_timestamp(raw_time, shift),
                open=float(pick(row, "open", "o")),
                high=float(pick(row, "high", "h")),
                low=float(pick(row, "low", "l")),
                close=float(pick(row, "close", "c")),
                volume=float(pick(row, "volume", "vol", "v") or 0),
                turnover=float(pick(row, "turnover", "amount") or 0),
                symbol=row_symbol or symbol,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"无法解析行情行: {row!r}") from exc
        values = (bar.open, bar.high, bar.low, bar.close)
        if not all(math.isfinite(value) and value > 0 for value in values):
            raise ValueError(f"{bar.key} 存在非正数或非有限OHLC")
        by_key[bar.key] = bar
    bars = sorted(by_key.values(), key=lambda item: item.key)
    if not bars:
        raise ValueError(f"未从 {path} 读到匹配 {symbol or '任意标的'} 的行情")
    return bars


def regression_return_score(closes: list[float]) -> float:
    """Return the fitted log-price move across N completed 15-minute bars."""
    if len(closes) < 2:
        raise ValueError("强度 N 必须至少为2")
    if not all(math.isfinite(value) and value > 0 for value in closes):
        raise ValueError("强度计算包含非正数或非有限收盘价")
    values = [math.log(value) for value in closes]
    center = (len(values) - 1) / 2
    x_centered = [index - center for index in range(len(values))]
    denominator = math.fsum(value * value for value in x_centered)
    if denominator <= 1e-12:
        raise ValueError("强度回归自变量退化")
    mean_y = statistics.mean(values)
    slope = math.fsum(
        x_value * (y_value - mean_y)
        for x_value, y_value in zip(x_centered, values)
    ) / denominator
    return math.expm1(slope * (len(values) - 1))


def rank_strength(
    bars_by_symbol: dict[str, list[Bar]],
    signal_key: str,
    n: int,
) -> dict[str, Any]:
    """Rank the four frozen ETFs using only common bars through signal_key."""
    if n < 2:
        raise ValueError("--n/--strength-n 必须至少为2")
    if set(bars_by_symbol) != set(STRENGTH_SYMBOLS):
        missing = sorted(set(STRENGTH_SYMBOLS) - set(bars_by_symbol))
        extra = sorted(set(bars_by_symbol) - set(STRENGTH_SYMBOLS))
        raise ValueError(f"强度标的集合错误，缺少={missing}，多余={extra}")
    closes: dict[str, dict[str, float]] = {}
    common_keys: Optional[set[str]] = None
    for symbol in STRENGTH_SYMBOLS:
        values = {
            bar.key: bar.close
            for bar in bars_by_symbol[symbol]
            if bar.key <= signal_key
        }
        closes[symbol] = values
        keys = set(values)
        common_keys = keys if common_keys is None else common_keys & keys
    keys = sorted(common_keys or set())[-n:]
    if len(keys) != n:
        raise ValueError(
            f"{signal_key} 前四ETF共同15分钟K线仅 {len(keys)} 根，"
            f"不足 N={n}"
        )
    order = {symbol: index for index, symbol in enumerate(STRENGTH_SYMBOLS)}
    scores = {
        symbol: regression_return_score(
            [closes[symbol][key] for key in keys]
        )
        for symbol in STRENGTH_SYMBOLS
    }
    ranked_symbols = sorted(
        STRENGTH_SYMBOLS,
        key=lambda symbol: (-scores[symbol], order[symbol]),
    )
    return {
        "n": n,
        "formula": STRENGTH_FORMULA,
        "observation_start": keys[0],
        "observation_end": keys[-1],
        "ranking": [
            {"rank": index + 1, "symbol": symbol, "score": scores[symbol]}
            for index, symbol in enumerate(ranked_symbols)
        ],
    }


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


def latest_feature(bars: list[Bar]) -> Optional[Feature]:
    required = VOLATILITY_BARS + 2
    if len(bars) < required:
        return None
    return build_features(bars[-required:])[-1]


def decide_at_close(
    position: int,
    t1_sellable: bool,
    clock: str,
    feature: Optional[Feature],
    threshold: Any,
    t1_sell_mode: str = T1_SELL_MODE,
) -> tuple[bool, bool, Optional[str], bool]:
    """Return raw signals, next-open action and same-day deferred sell."""
    if t1_sell_mode not in T1_SELL_MODES:
        raise ValueError(f"t1_sell_mode 必须是 {', '.join(T1_SELL_MODES)}")
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
        t1_sell_mode == "defer-next-open"
        and position == 1
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


def month_sequence(start: str, end: str) -> list[date]:
    current = first_of_month(start)
    stop = first_of_month(end)
    result: list[date] = []
    while current <= stop:
        result.append(current)
        current = shift_months(current, 1)
    return result


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
    t1_sell_mode: str = T1_SELL_MODE,
) -> Evaluation:
    if not bars:
        raise ValueError("训练窗口没有行情")
    if t1_sell_mode not in T1_SELL_MODES:
        raise ValueError(f"t1_sell_mode 必须是 {', '.join(T1_SELL_MODES)}")
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
            t1_sell_mode == "defer-next-open"
            and deferred_t1_sell
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
                t1_sell_mode,
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
    t1_sell_mode: str = T1_SELL_MODE,
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
                t1_sell_mode,
            )
            cache[candidate] = evaluation
        constraint = constraint_cache.get(candidate)
        if constraint is None:
            constraint = evaluate_candidate(
                bars,
                features,
                candidate,
                CONSTRAINT_COST_BPS,
                t1_sell_mode,
            )
            constraint_cache[candidate] = constraint
        if constraint.strategy_return >= MIN_CONSTRAINT_RETURN:
            rows.append((candidate, evaluation))
    return choose_global_optimum(rows)


def optimize_exhaustive_vectorized(
    bars: list[Bar],
    features: list[Optional[Feature]],
    t1_sell_mode: str = T1_SELL_MODE,
) -> tuple[Candidate, Evaluation, int, Evaluation]:
    """Evaluate all 8,008 candidates exactly with vectorized state arrays.

    Candidate states are independent; NumPy only evaluates them in parallel.
    The path, next-bar execution, T+1 rule, objective and tie-break are the
    same as ``evaluate_candidate``. The chosen candidate is re-evaluated by
    that scalar implementation before publication.
    """
    if t1_sell_mode not in T1_SELL_MODES:
        raise ValueError(f"t1_sell_mode 必须是 {', '.join(T1_SELL_MODES)}")
    if np is None:
        cache: dict[Candidate, Evaluation] = {}
        constraint_cache: dict[Candidate, Evaluation] = {}
        candidate, evaluation = optimize_exhaustive(
            bars, features, cache, constraint_cache, t1_sell_mode
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
        if t1_sell_mode == "defer-next-open":
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
                if t1_sell_mode == "defer-next-open":
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
        bars, features, candidate, CALIBRATION_COST_BPS, t1_sell_mode
    )
    constraint = evaluate_candidate(
        bars, features, candidate, CONSTRAINT_COST_BPS, t1_sell_mode
    )
    return candidate, evaluation, count, constraint


def calibrate_month(
    bars: list[Bar],
    features: list[Optional[Feature]],
    cutoff: date,
    window_months: int = WINDOW_MONTHS,
    t1_sell_mode: str = T1_SELL_MODE,
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

    return optimize_exhaustive_vectorized(
        train_bars, train_features, t1_sell_mode
    )


def build_schedule(
    bars: list[Bar],
    features: list[Optional[Feature]],
    start: str,
    end: str,
    window_months: int = WINDOW_MONTHS,
    t1_sell_mode: str = T1_SELL_MODE,
) -> tuple[dict[str, Threshold], list[dict[str, Any]]]:
    schedule: dict[str, Threshold] = {}
    audits: list[dict[str, Any]] = []
    for cutoff in month_sequence(start, end):
        candidate, evaluation, count, constraint = calibrate_month(
            bars, features, cutoff, window_months, t1_sell_mode
        )
        month = cutoff.strftime("%Y-%m")
        schedule[month] = replace(candidate, month=month)
        audits.append(
            {
                "strategy": STRATEGY,
                "version": VERSION,
                "month": month,
                "window_months": window_months,
                "t1_sell_mode": t1_sell_mode,
                "window_start": shift_months(
                    cutoff, -window_months
                ).isoformat(),
                "window_end": cutoff.fromordinal(
                    cutoff.toordinal() - 1
                ).isoformat(),
                "entry_z30": candidate.entry_z30,
                "entry_z60": candidate.entry_z60,
                "exit_z30": candidate.exit_z30,
                "exit_z60": candidate.exit_z60,
                "train_score": evaluation.score,
                "train_strategy_return": evaluation.strategy_return,
                "train_benchmark_return": evaluation.benchmark_return,
                "train_simple_excess": evaluation.simple_excess,
                "train_strategy_sortino": evaluation.strategy_sortino,
                "train_max_drawdown": evaluation.max_drawdown,
                "train_exposure": evaluation.exposure,
                "train_buys": evaluation.buys,
                "train_sells": evaluation.sells,
                "constraint_cost_bps": CONSTRAINT_COST_BPS,
                "constraint_strategy_return": constraint.strategy_return,
                "search_method": SEARCH_METHOD,
                "search_evaluations": count,
                "grid_points": GRID_POINTS,
            }
        )
        eprint(
            f"calibrated {month} threshold={candidate} "
            f"score={evaluation.score:.6f}"
        )
    return schedule, audits


def load_threshold_schedule(path: Path) -> dict[str, Threshold]:
    raw = read_records(path)
    if isinstance(raw, dict) and isinstance(raw.get("threshold"), dict):
        published = dict(raw["threshold"])
        published.setdefault("month", raw.get("month"))
        published.setdefault("source", raw.get("version", VERSION))
        rows = [published]
    else:
        rows = unwrap_records(
            raw, ("thresholds", "schedule", "records", "data")
        )
    result: dict[str, Threshold] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        month = str(pick(row, "month", "calibration_month") or "")[:7]
        if not month:
            continue
        result[month] = Threshold(
            month=month,
            entry_z30=float(pick(row, "entry_z30")),
            entry_z60=float(pick(row, "entry_z60")),
            exit_z30=float(pick(row, "exit_z30")),
            exit_z60=float(pick(row, "exit_z60")),
            source=str(pick(row, "source") or VERSION),
        )
    if not result:
        raise ValueError(f"{path} 中没有有效月度阈值")
    return result


class ThresholdProvider:
    def __init__(
        self,
        schedule: dict[str, Threshold],
        allow_freeze: bool = False,
    ) -> None:
        self.schedule = schedule
        self.allow_freeze = allow_freeze
        self.warned: set[str] = set()

    def for_date(self, value: str) -> Threshold:
        month = value[:7]
        if month in self.schedule:
            return self.schedule[month]
        if not self.allow_freeze:
            raise ValueError(f"缺少 {month} 阈值；应先完成当月校准")
        eligible = [key for key in self.schedule if key <= month]
        if not eligible:
            raise ValueError(f"{month} 之前没有可冻结阈值")
        source_month = max(eligible)
        if month not in self.warned:
            eprint(f"警告: {month} 沿用 {source_month} 阈值")
            self.warned.add(month)
        source = self.schedule[source_month]
        return replace(source, month=month, source=f"frozen:{source_month}")


def load_live_threshold_provider(
    path: Path,
    symbol: str,
    allow_freeze: bool,
    window_months: int = WINDOW_MONTHS,
    t1_sell_mode: str = T1_SELL_MODE,
) -> ThresholdProvider:
    """Load and validate an audited monthly publication for live inference."""
    raw = read_records(path)
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
    if raw.get("t1_sell_mode") != t1_sell_mode:
        raise ValueError(
            f"阈值T+1退出模式不是本次指定的 {t1_sell_mode}"
        )
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
    schedule = load_threshold_schedule(path)
    return ThresholdProvider(schedule, allow_freeze=allow_freeze)


class ThresholdDirectoryProvider:
    """Load audited monthly publications lazily so live need not restart."""

    def __init__(
        self,
        directory: Path,
        symbol: str,
        allow_freeze: bool,
        window_months: int = WINDOW_MONTHS,
        t1_sell_mode: str = T1_SELL_MODE,
    ) -> None:
        self.directory = directory
        self.symbol = symbol
        self.allow_freeze = allow_freeze
        self.window_months = window_months
        self.t1_sell_mode = t1_sell_mode
        self.providers: dict[str, tuple[tuple[int, int], ThresholdProvider]] = {}
        self.warned: set[str] = set()
        if not directory.is_dir():
            raise ValueError(f"阈值目录不存在: {directory}")

    def publication_path(self, month: str) -> Path:
        return self.directory / f"threshold_{month}.json"

    def load_month(self, month: str) -> ThresholdProvider:
        path = self.publication_path(month)
        stat = path.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
        cached = self.providers.get(month)
        if cached is not None and cached[0] == signature:
            return cached[1]
        provider = load_live_threshold_provider(
            path,
            symbol=self.symbol,
            allow_freeze=False,
            window_months=self.window_months,
            t1_sell_mode=self.t1_sell_mode,
        )
        if month not in provider.schedule:
            raise ValueError(f"{path} 发布月份不是文件名声明的 {month}")
        self.providers[month] = (signature, provider)
        return provider

    def latest_published_month(self, month: str) -> Optional[str]:
        candidates = []
        for path in self.directory.glob("threshold_????-??.json"):
            candidate = path.stem.removeprefix("threshold_")
            if candidate <= month:
                candidates.append(candidate)
        return max(candidates) if candidates else None

    def for_date(self, value: str) -> Threshold:
        month = value[:7]
        path = self.publication_path(month)
        if path.is_file():
            return self.load_month(month).for_date(value)
        if not self.allow_freeze:
            raise ValueError(
                f"缺少 {path}；应由 live 自动发布当月阈值"
            )
        source_month = self.latest_published_month(month)
        if source_month is None:
            raise ValueError(f"{month} 之前没有可冻结阈值")
        source = self.load_month(source_month).for_date(
            f"{source_month}-01"
        )
        if month not in self.warned:
            eprint(f"警告: {month} 沿用 {source_month} 阈值")
            self.warned.add(month)
        return replace(source, month=month, source=f"frozen:{source_month}")


def in_range(key: str, start: str, end: str) -> bool:
    return start <= key[:10] <= end


def generate_actions(
    bars: list[Bar],
    features: list[Optional[Feature]],
    provider: ThresholdProvider,
    start: str,
    end: str,
    t1_sell_mode: str = T1_SELL_MODE,
) -> list[Action]:
    if t1_sell_mode not in T1_SELL_MODES:
        raise ValueError(f"t1_sell_mode 必须是 {', '.join(T1_SELL_MODES)}")
    test_dates = sorted(
        {bar.trading_date for bar in bars if in_range(bar.key, start, end)}
    )
    session_index = {value: index for index, value in enumerate(test_dates)}
    actions: list[Action] = []
    state = 0
    pending: Optional[dict[str, str]] = None
    deferred_t1_sell: Optional[dict[str, str]] = None
    entry_session: Optional[int] = None
    for index, bar in enumerate(bars):
        if not in_range(bar.key, start, end):
            continue
        session = session_index[bar.trading_date]
        deferred_ready = bool(
            t1_sell_mode == "defer-next-open"
            and deferred_t1_sell is not None
            and bar.trading_date > deferred_t1_sell["signal_date"]
        )
        deferred_execution = deferred_ready and state == 1
        execution_side = (
            "SELL"
            if deferred_execution
            else pending["side"] if pending is not None else None
        )
        execution_source = deferred_t1_sell if deferred_execution else pending
        if deferred_ready:
            deferred_t1_sell = None
        if execution_side is not None and execution_source is not None:
            actions.append(
                Action(
                    side=execution_side,
                    signal_key=execution_source["signal_key"],
                    execution_key=bar.key,
                    execution_price=bar.open,
                    calibration_month=execution_source["calibration_month"],
                )
            )
            state = 1 if execution_side == "BUY" else 0
            entry_session = session if state else None
            pending = None
        feature = features[index]
        if feature is None or index == len(bars) - 1:
            continue
        threshold = provider.for_date(bar.trading_date)
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
            threshold,
            t1_sell_mode,
        )
        if action is not None:
            pending = {
                "side": action,
                "signal_key": bar.key,
                "calibration_month": threshold.month,
            }
        elif defer_sell and deferred_t1_sell is None:
            deferred_t1_sell = {
                "signal_key": bar.key,
                "signal_date": bar.trading_date,
                "calibration_month": threshold.month,
            }
    return actions


def strategy_sortino_from_curve(curve: list[dict[str, Any]]) -> float:
    daily: dict[str, float] = {}
    for row in curve:
        daily[row["key"][:10]] = row["equity"]
    previous = 1.0
    returns: list[float] = []
    for day in sorted(daily):
        equity = daily[day]
        returns.append(equity / previous - 1)
        previous = equity
    downside = math.sqrt(
        statistics.mean(min(value, 0.0) ** 2 for value in returns)
    )
    if downside <= 1e-12:
        return math.nan
    return statistics.mean(returns) / downside * math.sqrt(252)


def simulate(
    bars: list[Bar],
    actions: list[Action],
    start: str,
    end: str,
    cost_bps: float,
    window_months: int = WINDOW_MONTHS,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    test_bars = [bar for bar in bars if in_range(bar.key, start, end)]
    if not test_bars:
        raise ValueError("回测区间没有行情")
    actions_by_key = {action.execution_key: action for action in actions}
    cost = cost_bps / 10000
    cash = 1.0
    units = 0.0
    exposure_bars = 0
    curve: list[dict[str, Any]] = []
    for bar in test_bars:
        action = actions_by_key.get(bar.key)
        if action is not None and action.side == "BUY":
            units = cash * (1 - cost) / bar.open
            cash = 0.0
        elif action is not None and action.side == "SELL":
            cash = units * bar.open * (1 - cost)
            units = 0.0
        if units > 0:
            exposure_bars += 1
        curve.append(
            {
                "key": bar.key,
                "equity": cash + units * bar.close,
                "benchmark": bar.close / test_bars[0].open,
                "position": int(units > 0),
            }
        )
    last = curve[-1]
    total_return = last["equity"] - 1
    benchmark_return = last["benchmark"] - 1
    metrics = {
        "strategy": STRATEGY,
        "strategy_status": STRATEGY_STATUS,
        "version": VERSION,
        "test_start": start,
        "test_end": end,
        "symbol": test_bars[0].symbol,
        "cost_bps_per_side": cost_bps,
        "bars": len(test_bars),
        "total_return": total_return,
        "benchmark_return": benchmark_return,
        "excess_difference": total_return - benchmark_return,
        "relative_excess": last["equity"] / last["benchmark"] - 1,
        "max_drawdown": max_drawdown([row["equity"] for row in curve]),
        "benchmark_max_drawdown": max_drawdown(
            [row["benchmark"] for row in curve]
        ),
        "strategy_sortino": strategy_sortino_from_curve(curve),
        "exposure": exposure_bars / len(test_bars),
        "buys": sum(action.side == "BUY" for action in actions),
        "sells": sum(action.side == "SELL" for action in actions),
        "ending_position": curve[-1]["position"],
        "window_months": window_months,
        "grid_points": GRID_POINTS,
        "objective": "held_market_downside_ratio",
        "calibration_cost_bps": CALIBRATION_COST_BPS,
        "constraint_cost_bps": CONSTRAINT_COST_BPS,
    }
    return metrics, curve


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_outputs(
    output_dir: Path,
    metrics: dict[str, Any],
    curve: list[dict[str, Any]],
    actions: list[Action],
    schedule: dict[str, Threshold],
    audits: list[dict[str, Any]],
    strength_rankings: Optional[list[dict[str, Any]]] = None,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_csv(output_dir / "equity.csv", curve)
    write_csv(output_dir / "trades.csv", [asdict(action) for action in actions])
    write_csv(
        output_dir / "threshold_schedule.csv",
        [asdict(schedule[key]) for key in sorted(schedule)],
    )
    write_csv(output_dir / "calibration_audit.csv", audits)
    if strength_rankings is not None:
        write_csv(output_dir / "strength_rankings.csv", strength_rankings)


def build_strength_rankings(
    actions: list[Action],
    bars_by_symbol: dict[str, list[Bar]],
    n: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for action in actions:
        if action.side != "BUY":
            continue
        result = rank_strength(bars_by_symbol, action.signal_key, n)
        row: dict[str, Any] = {
            "signal_key": action.signal_key,
            "execution_key": action.execution_key,
            "n": n,
            "formula": STRENGTH_FORMULA,
            "observation_start": result["observation_start"],
            "observation_end": result["observation_end"],
        }
        for item in result["ranking"]:
            rank = int(item["rank"])
            row[f"rank_{rank}_symbol"] = item["symbol"]
            row[f"rank_{rank}_score"] = item["score"]
        rows.append(row)
    return rows


def run_backtest(args: argparse.Namespace) -> int:
    bars_path = Path(args.bars_file).resolve()
    bars = load_bars(
        bars_path,
        symbol=args.symbol,
        time_convention=args.bar_time_convention,
    )
    start = args.start
    end = args.end or bars[-1].trading_date
    if start > end:
        raise ValueError("start 晚于 end")
    features = build_features(bars)
    t1_sell_mode = getattr(args, "t1_sell_mode", T1_SELL_MODE)
    schedule, audits = build_schedule(
        bars,
        features,
        start,
        end,
        args.window_months,
        t1_sell_mode,
    )
    provider = ThresholdProvider(schedule)
    actions = generate_actions(
        bars, features, provider, start, end, t1_sell_mode
    )
    metrics, curve = simulate(
        bars,
        actions,
        start,
        end,
        args.cost_bps,
        args.window_months,
    )
    metrics["bars_file"] = str(bars_path)
    metrics["t1_sell_mode"] = t1_sell_mode
    metrics["deferred_t1_sells"] = sum(
        action.side == "SELL"
        and action.signal_key[:10] != action.execution_key[:10]
        for action in actions
    )
    strength_paths = dict(args.strength_bars or [])
    strength_rankings: Optional[list[dict[str, Any]]] = None
    if strength_paths:
        if set(strength_paths) != set(STRENGTH_SYMBOLS):
            missing = sorted(set(STRENGTH_SYMBOLS) - set(strength_paths))
            raise ValueError(
                "--strength-bars 必须同时提供四只ETF；"
                f"缺少 {missing}"
            )
        strength_bars = {
            symbol: load_bars(
                path,
                symbol=symbol,
                time_convention=args.bar_time_convention,
            )
            for symbol, path in strength_paths.items()
        }
        strength_rankings = build_strength_rankings(
            actions, strength_bars, args.strength_n
        )
        metrics["strength_ranking"] = {
            "enabled": True,
            "n": args.strength_n,
            "formula": STRENGTH_FORMULA,
            "adjustment": "Futu QFQ expected",
            "buy_signals": len(strength_rankings),
            "latest": strength_rankings[-1] if strength_rankings else None,
        }
    else:
        metrics["strength_ranking"] = {
            "enabled": False,
            "n": args.strength_n,
            "formula": STRENGTH_FORMULA,
            "reason": "未提供四个 --strength-bars；本次仅回测择时",
        }
    if args.output_dir:
        write_outputs(
            Path(args.output_dir).resolve(),
            metrics,
            curve,
            actions,
            schedule,
            audits,
            strength_rankings,
        )
    if args.json:
        print(json.dumps(metrics, ensure_ascii=False, indent=2))
    else:
        print(f"{VERSION} 回测完成")
        print(
            f"参数: WINDOW_MONTHS={args.window_months}, "
            f"N={args.strength_n}, T1_SELL_MODE={t1_sell_mode}"
        )
        print(f"区间: {start} 至 {end}")
        print(f"策略收益: {metrics['total_return']:.4%}")
        print(f"基准收益: {metrics['benchmark_return']:.4%}")
        print(f"简单超额: {metrics['excess_difference']:.4%}")
        print(f"最大回撤: {metrics['max_drawdown']:.4%}")
        print(f"Sortino: {metrics['strategy_sortino']:.4f}")
        if strength_rankings is None:
            print("强度排名: 未启用（未提供四个 --strength-bars）")
        else:
            latest = strength_rankings[-1] if strength_rankings else None
            print(
                "强度排名: "
                + (
                    f"{len(strength_rankings)} 个买入信号，"
                    f"最近第一名 {latest['rank_1_symbol']}"
                    if latest
                    else "没有买入信号"
                )
            )
    return 0


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
    rows: list[dict[str, Any]] = []
    page_key = None
    with managed_futu_context(
        OpenQuoteContext,
        host=host,
        port=port,
    ) as context:
        while True:
            ret, data, page_key = context.request_history_kline(
                symbol,
                start=start,
                end=end,
                ktype=KLType.K_15M,
                autype=AuType.NONE,
                max_count=1000,
                page_req_key=page_key,
            )
            if ret != RET_OK:
                raise RuntimeError(f"获取历史15分钟K线失败: {data}")
            for index in range(len(data)):
                row = data.iloc[index] if hasattr(data, "iloc") else data[index]
                rows.append(
                    {
                        "code": str(row.get("code", symbol)),
                        "time_key": str(row.get("time_key", "")),
                        "open": float(row.get("open", 0)),
                        "high": float(row.get("high", 0)),
                        "low": float(row.get("low", 0)),
                        "close": float(row.get("close", 0)),
                        "volume": float(row.get("volume", 0) or 0),
                        "turnover": float(row.get("turnover", 0) or 0),
                    }
                )
            if page_key is None:
                break
            time.sleep(0.4)

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
    bar_time_convention: str,
    as_of: str,
    output: str,
    window_months: int = WINDOW_MONTHS,
    t1_sell_mode: str = T1_SELL_MODE,
) -> dict[str, Any]:
    bars_path = Path(bars_file).resolve()
    bars = load_bars(
        bars_path,
        symbol=symbol,
        time_convention=bar_time_convention,
    )
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
        bars, features, cutoff, window_months, t1_sell_mode
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
        "t1_sell_mode": t1_sell_mode,
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
        bar_time_convention=args.bar_time_convention,
        as_of=args.as_of,
        output=args.output,
        window_months=int(getattr(args, "window_months", WINDOW_MONTHS)),
        t1_sell_mode=getattr(args, "t1_sell_mode", T1_SELL_MODE),
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
    t1_sell_mode = getattr(args, "t1_sell_mode", T1_SELL_MODE)
    cutoff = current_market_month(now)
    month = cutoff.strftime("%Y-%m")
    publication = runtime.thresholds_dir / f"threshold_{month}.json"
    if publication.is_file():
        try:
            provider = load_live_threshold_provider(
                publication,
                symbol=args.symbol,
                allow_freeze=False,
                window_months=window_months,
                t1_sell_mode=t1_sell_mode,
            )
            provider.for_date(cutoff.isoformat())
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
        bar_time_convention=args.futu_time_convention,
        as_of=cutoff_text,
        output=str(publication),
        window_months=window_months,
        t1_sell_mode=t1_sell_mode,
    )
    provider = load_live_threshold_provider(
        publication,
        symbol=args.symbol,
        allow_freeze=False,
        window_months=window_months,
        t1_sell_mode=t1_sell_mode,
    )
    provider.for_date(cutoff.isoformat())
    return month, True


class LiveState:
    def __init__(
        self,
        path: Path,
        initial_position: str,
        entry_date: Optional[str],
    ) -> None:
        self.path = path
        migrated = False
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if raw.get("strategy") != STRATEGY:
                raise ValueError(
                    f"状态文件策略 {raw.get('strategy')!r} 与当前 "
                    f"{STRATEGY!r} 不一致"
                )
            stored_version = raw.get("version")
            if stored_version != VERSION and stored_version not in (
                LEGACY_STATE_VERSIONS
            ):
                raise ValueError(
                    f"状态文件版本 {stored_version!r} 与当前 "
                    f"{VERSION!r} 不一致"
                )
            migrated = stored_version != VERSION
            self.position = int(raw.get("position", 0))
            self.entry_date = raw.get("entry_date")
            self.pending = raw.get("pending")
            self.deferred_t1_sell = raw.get("deferred_t1_sell")
            self.signal_notification_dates = raw.get(
                "signal_notification_dates", {}
            )
        else:
            self.position = 1 if initial_position == "long" else 0
            self.entry_date = entry_date if self.position else None
            self.pending = None
            self.deferred_t1_sell = None
            self.signal_notification_dates: dict[str, str] = {}
            if self.position and not self.entry_date:
                raise ValueError("--initial-position long 必须同时给 --entry-date")
            self.save()
        if self.position not in {0, 1}:
            raise ValueError("状态文件 position 只能是0或1")
        if self.position and not self.entry_date:
            raise ValueError("多头状态缺少 entry_date，无法执行T+1")
        if self.deferred_t1_sell is not None:
            if not isinstance(self.deferred_t1_sell, dict):
                raise ValueError("状态文件 deferred_t1_sell 必须是对象或null")
            required = {"signal_key", "feature", "threshold"}
            if not required <= set(self.deferred_t1_sell):
                raise ValueError("状态文件 deferred_t1_sell 字段不完整")
            date.fromisoformat(str(self.deferred_t1_sell["signal_key"])[:10])
        if not isinstance(self.signal_notification_dates, dict):
            raise ValueError("状态文件 signal_notification_dates 必须是对象")
        for side, trading_date in self.signal_notification_dates.items():
            if side not in {"BUY", "SELL"}:
                raise ValueError("状态文件包含无效的信号通知方向")
            try:
                date.fromisoformat(str(trading_date))
            except ValueError as exc:
                raise ValueError("状态文件包含无效的信号通知日期") from exc
        if migrated:
            self.save()

    def signal_notified_on(self, side: str, trading_date: str) -> bool:
        return self.signal_notification_dates.get(side) == trading_date

    def mark_signal_notified(self, side: str, trading_date: str) -> None:
        self.signal_notification_dates[side] = trading_date

    def save(self) -> None:
        payload = {
            "strategy": STRATEGY,
            "version": VERSION,
            "position": self.position,
            "entry_date": self.entry_date,
            "pending": self.pending,
            "deferred_t1_sell": self.deferred_t1_sell,
            "signal_notification_dates": self.signal_notification_dates,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        write_json_atomic(self.path, payload)


class LiveEventNotifier:
    """Send selected live events without blocking the Futu callback thread."""

    def __init__(
        self,
        notification_engine: Any,
        notify_lifecycle: bool = False,
        maxsize: int = 100,
    ) -> None:
        self.engine = notification_engine
        self.notify_lifecycle = notify_lifecycle
        self.queue: queue.Queue[Optional[dict[str, Any]]] = queue.Queue(
            maxsize=maxsize
        )
        self.last_enqueued: dict[str, float] = {}
        self.thread = threading.Thread(
            target=self._run,
            name="m1-live-notifier",
            daemon=True,
        )
        self.thread.start()

    def notify(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        signal = event_type == "SIGNAL" and event.get("action") in {
            "BUY", "SELL"
        }
        lifecycle = self.notify_lifecycle and event_type in {
            "READY", "STOPPED", "THRESHOLD_READY"
        }
        if event_type != "ERROR" and not signal and not lifecycle:
            return
        now = time.monotonic()
        key = "|".join(
            str(event.get(name, ""))
            for name in ("type", "action", "bar_key", "message")
        )
        if now - self.last_enqueued.get(key, -math.inf) < 300:
            return
        self.last_enqueued[key] = now
        try:
            self.queue.put_nowait(dict(event))
        except queue.Full:
            eprint(f"通知队列已满，丢弃事件: {key}")

    def format_event(self, event: dict[str, Any]) -> tuple[str, str]:
        event_type = str(event.get("type", "UNKNOWN"))
        symbol = str(event.get("symbol", ""))
        action = str(event.get("action", ""))
        label = action if event_type == "SIGNAL" else event_type
        subject = f"M1 {symbol} {label}".strip()
        if event_type == "SIGNAL":
            feature = event.get("feature") or {}
            threshold = event.get("threshold") or {}
            strength = event.get("strength") or {}
            ranked = strength.get("ranking") or []
            ranking_text = " > ".join(
                f"{item.get('symbol')}({float(item.get('score', math.nan)):.3%})"
                for item in ranked
            )
            message = (
                f"[M1] {symbol} {action}\n"
                f"信号K线: {event.get('bar_key')}\n"
                f"z30/z60: {float(feature.get('z30', math.nan)):.3f} / "
                f"{float(feature.get('z60', math.nan)):.3f}\n"
                f"阈值月份: {threshold.get('month', '')}\n"
                f"参数: WINDOW_MONTHS={event.get('window_months')}, "
                f"N={event.get('strength_n')}\n"
                f"信号前仓位: {event.get('position_before')}"
            )
            if action == "BUY":
                message += f"\n强度排名: {ranking_text}"
        elif event_type == "ERROR":
            message = f"[M1] {symbol} 实时推理错误\n{event.get('message', '')}"
        else:
            message = (
                f"[M1] {symbol} {event_type}\n"
                f"仓位状态: {event.get('position')}\n"
                f"时间: {event.get('emitted_at', '')}"
            )
        return subject, message

    def _run(self) -> None:
        while True:
            event = self.queue.get()
            if event is None:
                self.queue.task_done()
                return
            try:
                subject, message = self.format_event(event)
                self._send(subject, message)
            except Exception as exc:
                eprint(f"通知处理失败: {exc}")
            self.queue.task_done()

    def _send(self, subject: str, message: str) -> None:
        senders = (
            self.engine.send_webhook,
            lambda text: self.engine.send_telegram_message(
                text, "https://www.futunn.com/"
            ),
            lambda text: self.engine.send_email(subject, text),
        )
        for send in senders:
            try:
                send(message)
            except Exception as exc:
                eprint(f"通知发送失败: {exc}")

    def close(self, timeout: float = 10.0) -> None:
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            eprint("通知队列未清空，退出时无法等待全部通知")
            return
        self.thread.join(timeout=timeout)


def build_live_notifier(
    config_path: Optional[str],
    notify_lifecycle: bool,
) -> Optional[LiveEventNotifier]:
    if not config_path:
        return None
    path = Path(config_path).resolve()
    config = configparser.ConfigParser()
    if not config.read(path, encoding="utf-8"):
        raise ValueError(f"通知配置文件不存在或不可读: {path}")
    project_root = str(Path(__file__).resolve().parents[1])
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    from notification_engine import NotificationEngine

    return LiveEventNotifier(
        NotificationEngine(config),
        notify_lifecycle=notify_lifecycle,
    )


class LiveSignalEngine:
    def __init__(
        self,
        symbol: str,
        provider: Any,
        state: LiveState,
        time_convention: str,
        window_months: int = WINDOW_MONTHS,
        strength_n: int = STRENGTH_N,
        notification_mode: str = "position-aware",
        t1_sell_mode: str = T1_SELL_MODE,
        event_callback: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> None:
        self.symbol = symbol
        self.provider = provider
        self.state = state
        self.window_months = window_months
        self.strength_n = strength_n
        if notification_mode not in NOTIFICATION_MODES:
            raise ValueError(
                f"notification_mode 必须是 {', '.join(NOTIFICATION_MODES)}"
            )
        self.notification_mode = notification_mode
        if t1_sell_mode not in T1_SELL_MODES:
            raise ValueError(
                f"t1_sell_mode 必须是 {', '.join(T1_SELL_MODES)}"
            )
        self.t1_sell_mode = t1_sell_mode
        self.event_callback = event_callback
        self.shift_minutes = 15 if time_convention == "start" else 0
        self.completed: list[Bar] = []
        self.current: Optional[Bar] = None
        self.strength_bars: dict[str, list[Bar]] = {
            symbol: [] for symbol in STRENGTH_SYMBOLS
        }
        self.lock = threading.Lock()

    def emit(self, event: dict[str, Any]) -> None:
        payload = {
            "strategy": STRATEGY,
            "version": VERSION,
            "symbol": self.symbol,
            "window_months": self.window_months,
            "strength_n": self.strength_n,
            "t1_sell_mode": self.t1_sell_mode,
            "emitted_at": datetime.now().isoformat(timespec="seconds"),
            **event,
        }
        print(
            json.dumps(payload, ensure_ascii=False),
            flush=True,
        )
        if self.event_callback is not None:
            self.event_callback(payload)

    def bar_from_row(self, row: Any) -> Bar:
        getter = row.get if hasattr(row, "get") else lambda key, default=None: default
        bar = Bar(
            key=parse_timestamp(getter("time_key", ""), self.shift_minutes),
            open=float(getter("open", 0)),
            high=float(getter("high", 0)),
            low=float(getter("low", 0)),
            close=float(getter("close", 0)),
            volume=float(getter("volume", 0) or 0),
            turnover=float(getter("turnover", 0) or 0),
            symbol=str(getter("code", self.symbol)),
        )
        if min(bar.open, bar.high, bar.low, bar.close) <= 0:
            raise ValueError(f"{bar.key} 实时OHLC无效")
        return bar

    def bootstrap(self, rows: list[Any]) -> None:
        bars = [self.bar_from_row(row) for row in rows]
        bars = sorted({bar.key: bar for bar in bars}.values(), key=lambda bar: bar.key)
        if len(bars) < VOLATILITY_BARS + 6:
            raise ValueError(
                f"实时预热仅有 {len(bars)} 根，至少需要 "
                f"{VOLATILITY_BARS + 6} 根"
            )
        self.completed = bars[:-1]
        self.current = bars[-1]
        if (
            self.t1_sell_mode == "defer-next-open"
            and self.state.deferred_t1_sell
        ):
            opening_bars = [
                bar
                for bar in bars
                if bar.trading_date
                > str(self.state.deferred_t1_sell["signal_key"])[:10]
            ]
            if opening_bars:
                self.execute_deferred_t1_sell(opening_bars[0])
        if self.state.pending:
            next_bars = [
                bar
                for bar in bars
                if bar.key > str(self.state.pending.get("signal_key", ""))
            ]
            if next_bars:
                self.execute_pending(next_bars[0])
        self.emit(
            {
                "type": "READY",
                "history_bars": len(self.completed),
                "forming_bar": self.current.key,
                "position": self.state.position,
                "entry_date": self.state.entry_date,
                "strength": {
                    "symbols": list(STRENGTH_SYMBOLS),
                    "n": self.strength_n,
                    "formula": STRENGTH_FORMULA,
                    "adjustment": "QFQ",
                    "history_bars": {
                        symbol: len(self.strength_bars[symbol])
                        for symbol in STRENGTH_SYMBOLS
                    },
                },
                "active_threshold": asdict(
                    self.provider.for_date(self.current.trading_date)
                ),
            }
        )

    def bootstrap_strength(self, rows_by_symbol: dict[str, list[Any]]) -> None:
        if set(rows_by_symbol) != set(STRENGTH_SYMBOLS):
            raise ValueError("实时强度预热必须覆盖四只ETF")
        for symbol in STRENGTH_SYMBOLS:
            bars = [self.bar_from_row(row) for row in rows_by_symbol[symbol]]
            by_key = {bar.key: bar for bar in bars}
            ordered = [by_key[key] for key in sorted(by_key)]
            if len(ordered) < self.strength_n:
                raise ValueError(
                    f"{symbol} 实时预热仅有 {len(ordered)} 根，"
                    f"不足 N={self.strength_n}"
                )
            self.strength_bars[symbol] = ordered[-1200:]

    def current_strength(self, signal_key: str) -> dict[str, Any]:
        return rank_strength(self.strength_bars, signal_key, self.strength_n)

    def execute_pending(self, bar: Bar) -> None:
        pending = self.state.pending
        if not pending:
            return
        side = pending["side"]
        self.state.position = 1 if side == "BUY" else 0
        self.state.entry_date = bar.trading_date if side == "BUY" else None
        self.state.pending = None
        self.state.save()
        self.emit(
            {
                "type": "EXECUTION_ASSUMED",
                "side": side,
                "signal_key": pending["signal_key"],
                "execution_key": bar.key,
                "execution_price": bar.open,
                "position": self.state.position,
                "note": "仅更新信号状态，不发送订单",
            }
        )

    def execute_deferred_t1_sell(self, opening_bar: Bar) -> None:
        deferred = self.state.deferred_t1_sell
        if not deferred or self.t1_sell_mode != "defer-next-open":
            return
        signal_date = str(deferred["signal_key"])[:10]
        if opening_bar.trading_date <= signal_date:
            return
        position_before = self.state.position
        self.state.deferred_t1_sell = None
        if position_before != 1:
            self.state.save()
            return
        self.state.position = 0
        self.state.entry_date = None
        self.state.pending = None
        self.state.mark_signal_notified("SELL", opening_bar.trading_date)
        self.state.save()
        self.emit(
            {
                "type": "SIGNAL",
                "bar_key": deferred["signal_key"],
                "action": "SELL",
                "execution": "CURRENT_BAR_OPEN",
                "position_before": position_before,
                "t1_sellable": True,
                "feature": deferred["feature"],
                "threshold": deferred["threshold"],
                "strength": None,
            }
        )
        self.emit(
            {
                "type": "EXECUTION_ASSUMED",
                "side": "SELL",
                "signal_key": deferred["signal_key"],
                "execution_key": opening_bar.key,
                "execution_price": opening_bar.open,
                "position": self.state.position,
                "note": "T+1隔日开盘同步信号状态，不发送订单",
            }
        )

    def finalize(self, bar: Bar) -> None:
        if self.completed and bar.key <= self.completed[-1].key:
            return
        self.completed.append(bar)
        if len(self.completed) > 1200:
            self.completed = self.completed[-1200:]
        feature = latest_feature(self.completed)
        if feature is None or bar.clock not in ALL_DECISION_TIMES:
            return
        threshold = self.provider.for_date(bar.trading_date)
        t1_sellable = bool(
            self.state.position
            and self.state.entry_date
            and bar.trading_date > self.state.entry_date
        )
        buy_signal, sell_signal, execution_action, deferred_t1_sell = (
            decide_at_close(
                self.state.position,
                t1_sellable,
                bar.clock,
                feature,
                threshold,
                self.t1_sell_mode,
            )
        )
        actionable_buy = execution_action == "BUY"
        actionable_sell = execution_action == "SELL"
        if self.notification_mode == "position-independent":
            notify_buy = buy_signal and not self.state.signal_notified_on(
                "BUY", bar.trading_date
            )
            notify_sell = sell_signal and not self.state.signal_notified_on(
                "SELL", bar.trading_date
            )
        else:
            notify_buy = actionable_buy and not self.state.signal_notified_on(
                "BUY", bar.trading_date
            )
            notify_sell = (
                actionable_sell
                and not self.state.signal_notified_on("SELL", bar.trading_date)
            )
        if deferred_t1_sell:
            notify_sell = False
        strength: Optional[dict[str, Any]] = None
        if notify_buy or actionable_buy:
            try:
                strength = self.current_strength(bar.key)
            except ValueError as exc:
                self.emit(
                    {
                        "type": "ERROR",
                        "message": f"强度排名不可用，BUY信号未发布: {exc}",
                    }
                )
                notify_buy = False
                actionable_buy = False
                execution_action = None
        state_changed = False
        if execution_action is not None:
            self.state.pending = {
                "side": execution_action,
                "signal_key": bar.key,
                "calibration_month": threshold.month,
            }
            state_changed = True
        if deferred_t1_sell and self.state.deferred_t1_sell is None:
            self.state.deferred_t1_sell = {
                "signal_key": bar.key,
                "feature": asdict(feature),
                "threshold": asdict(threshold),
            }
            state_changed = True
        if notify_buy:
            self.state.mark_signal_notified("BUY", bar.trading_date)
            state_changed = True
        if notify_sell:
            self.state.mark_signal_notified("SELL", bar.trading_date)
            state_changed = True
        if state_changed:
            self.state.save()
        notification_actions = []
        if notify_buy:
            notification_actions.append("BUY")
        if notify_sell:
            notification_actions.append("SELL")
        if not notification_actions:
            notification_actions.append("NONE")
        for action in notification_actions:
            self.emit(
                {
                    "type": "SIGNAL",
                    "bar_key": bar.key,
                    "action": action,
                    "execution": "NEXT_BAR_OPEN",
                    "position_before": self.state.position,
                    "t1_sellable": t1_sellable,
                    "feature": asdict(feature),
                    "threshold": asdict(threshold),
                    "strength": strength if action == "BUY" else None,
                }
            )

    def on_strength_bar(self, bar: Bar) -> None:
        with self.lock:
            symbol = canonical_strength_symbol(bar.symbol)
            by_key = {
                item.key: item for item in self.strength_bars[symbol]
            }
            by_key[bar.key] = bar
            self.strength_bars[symbol] = [
                by_key[key] for key in sorted(by_key)[-1200:]
            ]

    def on_bar(self, bar: Bar) -> None:
        with self.lock:
            if self.current is None:
                self.current = bar
                return
            if bar.key == self.current.key:
                self.current = bar
                return
            if bar.key < self.current.key:
                return
            prior = self.current
            self.finalize(prior)
            self.execute_deferred_t1_sell(bar)
            self.execute_pending(bar)
            self.current = bar


def _run_live(
    args: argparse.Namespace,
    notifier: Optional[LiveEventNotifier],
    runtime: LiveRuntimePaths,
    running_script_sha256: str,
    stopped: threading.Event,
) -> int:
    try:
        from futu import (
            AuType,
            CurKlineHandlerBase,
            KLType,
            OpenQuoteContext,
            RET_ERROR,
            RET_OK,
            SubType,
            SysNotifyHandlerBase,
            SysNotifyType,
        )
    except ImportError as exc:
        raise RuntimeError(
            "live 模式需要 futu-api；安装命令: pip install futu-api"
        ) from exc

    provider = ThresholdDirectoryProvider(
        runtime.thresholds_dir,
        symbol=args.symbol,
        allow_freeze=False,
        window_months=getattr(args, "window_months", WINDOW_MONTHS),
        t1_sell_mode=getattr(args, "t1_sell_mode", T1_SELL_MODE),
    )
    state = LiveState(
        runtime.state_file,
        args.initial_position,
        args.entry_date,
    )
    engine = LiveSignalEngine(
        args.symbol,
        provider,
        state,
        args.futu_time_convention,
        window_months=getattr(args, "window_months", WINDOW_MONTHS),
        strength_n=getattr(args, "strength_n", STRENGTH_N),
        notification_mode=getattr(
            args, "notification_mode", "position-aware"
        ),
        t1_sell_mode=getattr(args, "t1_sell_mode", T1_SELL_MODE),
        event_callback=notifier.notify if notifier is not None else None,
    )
    fatal_errors: queue.SimpleQueue[str] = queue.SimpleQueue()

    def record_fatal(message: str) -> None:
        fatal_errors.put(message)

    target_symbol = normalize_symbol(args.symbol)
    strength_symbols = {normalize_symbol(symbol) for symbol in STRENGTH_SYMBOLS}

    class Handler(CurKlineHandlerBase):
        def on_recv_rsp(self, rsp_pb: Any) -> tuple[int, Any]:
            ret_code, data = super().on_recv_rsp(rsp_pb)
            if ret_code != RET_OK:
                record_fatal(f"Futu K线回调失败: {data}")
                return RET_ERROR, data
            try:
                for index in range(len(data)):
                    row = data.iloc[index] if hasattr(data, "iloc") else data[index]
                    bar = engine.bar_from_row(row)
                    normalized = normalize_symbol(bar.symbol)
                    if normalized == target_symbol:
                        engine.on_bar(bar)
                    elif normalized in strength_symbols:
                        engine.on_strength_bar(bar)
            except Exception as exc:
                record_fatal(f"实时K线处理失败: {exc!r}")
                return RET_ERROR, data
            return RET_OK, data

    class SystemHandler(SysNotifyHandlerBase):
        def on_recv_rsp(self, rsp_pb: Any) -> tuple[int, Any]:
            ret_code, content = super().on_recv_rsp(rsp_pb)
            if ret_code != RET_OK:
                record_fatal(f"Futu 系统通知失败: {content}")
                return RET_ERROR, content
            if not isinstance(content, tuple) or len(content) != 3:
                record_fatal(f"Futu 系统通知格式无效: {content!r}")
                return RET_ERROR, content
            notify_type, _sub_type, data = content
            if (
                notify_type == SysNotifyType.CONN_STATUS
                and isinstance(data, dict)
                and data.get("qot_logined") is False
            ):
                record_fatal("Futu 行情连接已断开")
            return RET_OK, content

    host, port = resolve_live_connection(args.config)
    with managed_futu_context(
        OpenQuoteContext,
        host=host,
        port=port,
    ) as context:
        subscribed_symbols = [args.symbol, *STRENGTH_SYMBOLS]
        ret, message = context.subscribe(
            subscribed_symbols, [SubType.K_15M], subscribe_push=False
        )
        if ret != RET_OK:
            raise RuntimeError(f"订阅预热失败: {message}")
        strength_rows: dict[str, list[Any]] = {}
        for symbol in STRENGTH_SYMBOLS:
            ret, strength_data = context.get_cur_kline(
                symbol,
                max(
                    args.history_bars,
                    getattr(args, "strength_n", STRENGTH_N) + 1,
                ),
                KLType.K_15M,
                AuType.QFQ,
            )
            if ret != RET_OK:
                raise RuntimeError(f"获取 {symbol} QFQ 预热K线失败: {strength_data}")
            strength_rows[symbol] = [
                strength_data.iloc[index]
                if hasattr(strength_data, "iloc")
                else strength_data[index]
                for index in range(len(strength_data))
            ]
        engine.bootstrap_strength(strength_rows)
        ret, data = context.get_cur_kline(
            args.symbol,
            args.history_bars,
            KLType.K_15M,
            AuType.NONE,
        )
        if ret != RET_OK:
            raise RuntimeError(f"获取预热K线失败: {data}")
        rows = [
            data.iloc[index] if hasattr(data, "iloc") else data[index]
            for index in range(len(data))
        ]
        engine.bootstrap(rows)
        if context.set_handler(Handler()) != RET_OK:
            raise RuntimeError("注册Futu K线回调失败")
        if context.set_handler(SystemHandler()) != RET_OK:
            raise RuntimeError("注册Futu系统回调失败")
        ret, message = context.subscribe(
            subscribed_symbols, [SubType.K_15M], subscribe_push=True
        )
        if ret != RET_OK:
            raise RuntimeError(f"开启K线推送失败: {message}")
        deadline = (
            time.monotonic() + args.duration
            if args.duration > 0
            else None
        )
        ensured_month = current_market_month().strftime("%Y-%m")
        next_maintenance = (
            time.monotonic() + LIVE_MAINTENANCE_INTERVAL_SECONDS
        )
        while not stopped.wait(1.0):
            if not fatal_errors.empty():
                raise RuntimeError(fatal_errors.get_nowait())
            if (
                sha256_file(Path(__file__).resolve())
                != running_script_sha256
            ):
                raise RuntimeError(
                    "脚本文件在 live 运行期间发生变化；退出并由PM2重启"
                )
            now_monotonic = time.monotonic()
            if deadline is not None and now_monotonic >= deadline:
                break
            month = current_market_month().strftime("%Y-%m")
            if (
                month != ensured_month
                and now_monotonic >= next_maintenance
            ):
                try:
                    ensured_month, generated = ensure_live_threshold(
                        args,
                        runtime,
                    )
                    engine.emit(
                        {
                            "type": "THRESHOLD_READY",
                            "month": ensured_month,
                            "generated": generated,
                        }
                    )
                except Exception as exc:
                    engine.emit(
                        {
                            "type": "ERROR",
                            "message": (
                                f"{month} 自动更新阈值失败，将重试: "
                                f"{exc!r}"
                            ),
                        }
                    )
                next_maintenance = (
                    now_monotonic + LIVE_MAINTENANCE_INTERVAL_SECONDS
                )
    engine.emit({"type": "STOPPED", "position": state.position})
    return 0


def run_live(args: argparse.Namespace) -> int:
    notifier = build_live_notifier(args.config, args.notify_lifecycle)
    try:
        runtime = LiveRuntimePaths.from_argument(args.runtime_dir)
        runtime.prepare()
        with graceful_stop_event() as stopped, RuntimeFileLock(
            runtime.lock_file
        ):
            running_script_sha256 = sha256_file(Path(__file__).resolve())
            month, generated = ensure_live_threshold(args, runtime)
            if stopped.is_set():
                return 0
            if notifier is not None and args.notify_lifecycle:
                notifier.notify(
                    {
                        "strategy": STRATEGY,
                        "version": VERSION,
                        "symbol": args.symbol,
                        "emitted_at": datetime.now().isoformat(
                            timespec="seconds"
                        ),
                        "type": "THRESHOLD_READY",
                        "month": month,
                        "generated": generated,
                    }
                )
            return _run_live(
                args,
                notifier,
                runtime,
                running_script_sha256,
                stopped,
            )
    except Exception as exc:
        if notifier is not None:
            notifier.notify(
                {
                    "strategy": STRATEGY,
                    "version": VERSION,
                    "symbol": args.symbol,
                    "emitted_at": datetime.now().isoformat(timespec="seconds"),
                    "type": "ERROR",
                    "message": repr(exc),
                }
            )
        raise
    finally:
        if notifier is not None:
            notifier.close()


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
        description=f"{VERSION} 单文件校准、回测与信号推理"
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
    t1_mode = option(
        "--t1-sell-mode",
        choices=T1_SELL_MODES,
        default=T1_SELL_MODE,
        help=(
            "同日买入后触发SELL的处理方式；defer-next-open在下一交易日"
            "首根15分钟K线开盘退出，ignore-same-day保留旧策略"
        ),
    )
    strength_n = option(
        "--n",
        "--strength-n",
        dest="strength_n",
        type=integer_at_least(2),
        default=STRENGTH_N,
        help=f"四ETF强度回归的固定15分钟K线根数（默认 {STRENGTH_N}）",
    )
    bar_arguments = [
        option("--bars-file", required=True, help="15分钟OHLC JSON/CSV"),
        option("--symbol", default="000902.SH"),
        option(
            "--bar-time-convention",
            choices=("end", "start"),
            default="end",
            help="输入时间代表K线结束或开始；start自动加15分钟",
        ),
    ]
    commands = {
        "backtest": (
            "按指定WINDOW_MONTHS校准并回测", run_backtest,
            bar_arguments
            + [
                window,
                t1_mode,
                strength_n,
                option("--start", required=True),
                option("--end"),
                option("--cost-bps", type=float, default=5.0),
                option("--output-dir"),
                option(
                    "--strength-bars",
                    type=parse_symbol_path,
                    action="append",
                    help=(
                        "可重复：SYMBOL=/absolute/path；同时提供四只ETF后，"
                        "为每个BUY输出固定N强度排名"
                    ),
                ),
                option("--json", action="store_true"),
            ],
        ),
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
                t1_mode,
                option("--as-of", required=True),
                option("--output", required=True, help="原子发布的月度阈值JSON路径"),
            ],
        ),
        "live": (
            "连接Futu OpenD并输出信号；不下单", run_live,
            [
                option("--symbol", required=True, help="例如 SH.000902"),
                option(
                    "--config",
                    help=(
                        "项目config.ini；复用Futu连接与"
                        "Telegram/Email/Webhook配置"
                    ),
                ),
                option("--history-bars", type=int, default=200),
                window,
                t1_mode,
                strength_n,
                option("--duration", type=int, default=0),
                option(
                    "--runtime-dir",
                    required=True,
                    help="绝对路径；保存行情、月度阈值、状态和实例锁",
                ),
                option(
                    "--initial-position",
                    choices=("flat", "long"),
                    default="flat",
                ),
                option("--entry-date"),
                option(
                    "--futu-time-convention",
                    choices=("end", "start"),
                    default="end",
                    help="Futu time_key口径；默认值为K线结束时间end",
                ),
                option(
                    "--notify-lifecycle",
                    action="store_true",
                    help="除交易信号和错误外，也通知READY/STOPPED",
                ),
                option(
                    "--notification-mode",
                    choices=NOTIFICATION_MODES,
                    default="position-aware",
                    help=(
                        "position-aware 保留原仓位约束通知；"
                        "position-independent 忽略仓位且同日"
                        "同方向仅通知一次"
                    ),
                ),
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
    if getattr(args, "cost_bps", 0) < 0:
        raise ValueError("--cost-bps 不能为负")
    return int(args.func(args))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        eprint(f"错误: {exc}")
        raise SystemExit(2)
