#!/usr/bin/env python3
"""M0: CSI Free Float price-only intraday timing strategy.

Strategy id: M0-v0.5-price-rolling9-held-downside.
``live`` emits signals and persists state; it never submits an order.
Production execution needs independent order, fill, reconciliation,
market-data integrity, alerting and kill-switch services.
Notification routing: BUY, SELL and ERROR events are pushed asynchronously
through the project ``NotificationEngine`` to the Webhook, Telegram and Email
channels configured in ``config.ini``; SIGNAL/NONE events are not pushed.
The Futu SDK logs under ``~/.com.futunn.FutuOpenD/Log`` on macOS; the service
account needs a writable home/log directory.

Paths in commands may be absolute; no command assumes a working directory.

Backtest::

    python /absolute/path/csi_flow_timing.py backtest \
      --bars-file /absolute/path/csi_flow_15m_futu.json \
      --symbol 000902.SH --start 2022-01-04 --end 2026-07-23 \
      --output-dir /absolute/path/m0_backtest

Generate one month's threshold::

    python /absolute/path/csi_flow_timing.py fetch-bars \
      --symbol SH.000902 --as-of 2026-07-01 \
      --config /absolute/path/config.ini \
      --output /absolute/path/csi_flow_15m_futu.json

    python /absolute/path/csi_flow_timing.py calibrate \
      --bars-file /absolute/path/csi_flow_15m_futu.json \
      --symbol 000902.SH --bar-time-convention end --as-of 2026-07-01 \
      --output /absolute/path/threshold_2026-07.json

Signal-only live inference::

    python /absolute/path/csi_flow_timing.py live \
      --symbol SH.000902 \
      --thresholds-dir /absolute/path/thresholds \
      --state-file /absolute/path/m0_live_state.json \
      --config /absolute/path/config.ini --futu-time-convention end

``live`` is a long-running process. Use a process supervisor (PM2, systemd or
launchd) for it; a system timer such as cron should only refresh
``threshold_YYYY-MM.json`` before the first decision point of a new month,
via the project wrapper ``market_analysis/run_csi_flow_calibration.sh``::

    PYTHON=/path/to/python /bin/bash market_analysis/run_csi_flow_calibration.sh
    # crontab (monthly, 00:15; escape % in crontab):
    15 0 1 * * PYTHON=/path/to/python /bin/bash /path/to/run_csi_flow_calibration.sh >> /path/to/cron_csi_flow_$(date +\%Y\%m\%d).log 2>&1

The wrapper reads environment variables for overrides:
``CSI_FLOW_FETCH_FROM_FUTU=0`` skips the Futu refresh and reuses the bars
file pointed to by ``CSI_FLOW_BARS_FILE`` (for pipelines that maintain their
own history); ``CSI_FLOW_BAR_TIME_CONVENTION`` defaults to ``end`` because
Futu's raw ``time_key`` is the bar's close time — set it to ``start`` only if
your OpenD returns bar open times.

Operational constraints (enforced by the code):

* Futu ``time_key`` convention (``--futu-time-convention``) must be verified
  against a known bar first; it shifts every bar key by 15 minutes and
  directly determines whether the 10:00, 10:30 and 14:00 checkpoints align.
* ``calibrate`` rejects a training window that does not end strictly before
  the target month, so a stale history file fails rather than publishing a
  silently outdated threshold.
* ``live`` does not exit by trading day or session: it holds the Futu K-line
  subscription until SIGINT/SIGTERM or ``--duration`` lapses, idles naturally
  outside RTH, and never backfills checkpoints missed while down. Run it
  24x7 and treat an in-session stop as an alert. If the environment forces
  daily start/stop, launch at 09:15 and SIGTERM at 15:15 on trading days,
  but also filter the Futu trading calendar for statutory holidays — a plain
  weekday cron (``1-5``) does not detect market closures.
* With the current month's threshold missing, ``live`` fails and notifies by
  default; ``--allow-threshold-freeze`` is an explicit operational downgrade
  that reuses the most recent published threshold.
* Threshold publications embed the SHA-256 of this script. After any change
  to ``csi_flow_timing.py``, republish the current month's threshold; the
  loader rejects a threshold whose recorded SHA does not match the running
  script.
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
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Optional


VERSION = "M0-v0.5-price-rolling9-held-downside-exact-grid"
PUBLISH_SCHEMA_VERSION = 1
SEARCH_METHOD = "exact_frozen_grid"
WINDOW_MONTHS = 9
VOLATILITY_BARS = 32
CALIBRATION_COST_BPS = 5.0
CONSTRAINT_COST_BPS = 10.0
MIN_CONSTRAINT_RETURN = 0.0
MIN_ROUND_TRIPS = 4
MIN_EXPOSURE = 0.10
MAX_EXPOSURE = 0.90
MAX_CALIBRATION_EDGE_GAP_DAYS = 15

ENTRY_TIMES = {"10:30", "14:00"}
EXIT_TIMES = {"10:00", "10:30", "14:00"}
ALL_DECISION_TIMES = ENTRY_TIMES | EXIT_TIMES

ENTRY_Z30_GRID = (-0.50, -0.25, 0.00, 0.25, 0.50, 0.75, 1.00, 1.25)
ENTRY_Z60_GRID = (-0.25, 0.00, 0.25, 0.50, 0.75, 1.00, 1.25, 1.50)
EXIT_Z30_GRID = (-1.50, -1.25, -1.00, -0.75, -0.50, -0.25, 0.00, 0.25)
EXIT_Z60_GRID = (-1.50, -1.25, -1.00, -0.75, -0.50, -0.25, 0.00, 0.25)

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
class Candidate:
    entry_z30: float
    entry_z60: float
    exit_z30: float
    exit_z60: float


@dataclass(frozen=True)
class Threshold:
    month: str
    entry_z30: float
    entry_z60: float
    exit_z30: float
    exit_z60: float
    source: str = VERSION


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
        if index < VOLATILITY_BARS + 1 or index < 4:
            result.append(None)
            continue
        sample = [
            value
            for value in log_returns[index - VOLATILITY_BARS : index]
            if value is not None
        ]
        sigma = sample_std(sample)
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
    index = len(bars) - 1
    if index < VOLATILITY_BARS + 1 or index < 4:
        return None
    sample = [
        math.log(bars[cursor].close / bars[cursor - 1].close)
        for cursor in range(index - VOLATILITY_BARS, index)
    ]
    sigma = sample_std(sample)
    if sigma is None or sigma <= 0:
        return None
    r30 = bars[index].close / bars[index - 2].close - 1
    r60 = bars[index].close / bars[index - 4].close - 1
    return Feature(
        z30=r30 / (sigma * math.sqrt(2)),
        z60=r60 / (sigma * 2),
        r30=r30,
        r60=r60,
        sigma=sigma,
    )


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
        if pending == "BUY":
            units = cash * (1 - cost) / bar.open
            cash = 0.0
            state = 1
            entry_session = session
            buys += 1
            pending = None
        elif pending == "SELL":
            cash = units * bar.open * (1 - cost)
            units = 0.0
            state = 0
            entry_session = None
            sells += 1
            pending = None

        feature = features[index]
        if feature is not None and index < len(bars) - 1:
            buy_score = (
                feature.z30 >= candidate.entry_z30
                and feature.z60 >= candidate.entry_z60
            )
            sell_score = (
                feature.z30 <= candidate.exit_z30
                and feature.z60 <= candidate.exit_z60
            )
            if state == 0 and bar.clock in ENTRY_TIMES and buy_score:
                pending = "BUY"
            elif (
                state == 1
                and entry_session is not None
                and session > entry_session
                and bar.clock in EXIT_TIMES
                and sell_score
            ):
                pending = "SELL"

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
    """Evaluate every point in the frozen grid and return the global optimum."""
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
                bars, features, candidate, CALIBRATION_COST_BPS
            )
            cache[candidate] = evaluation
        constraint = constraint_cache.get(candidate)
        if constraint is None:
            constraint = evaluate_candidate(
                bars, features, candidate, CONSTRAINT_COST_BPS
            )
            constraint_cache[candidate] = constraint
        if constraint.strategy_return >= MIN_CONSTRAINT_RETURN:
            rows.append((candidate, evaluation))
    return choose_global_optimum(rows)


def calibrate_month(
    bars: list[Bar],
    features: list[Optional[Feature]],
    cutoff: date,
) -> tuple[Candidate, Evaluation, int, Evaluation]:
    window_start = shift_months(cutoff, -WINDOW_MONTHS).isoformat()
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

    cache: dict[Candidate, Evaluation] = {}
    constraint_cache: dict[Candidate, Evaluation] = {}
    candidate, evaluation = optimize_exhaustive(
        train_bars,
        train_features,
        cache,
        constraint_cache,
    )
    return candidate, evaluation, len(cache), constraint_cache[candidate]


def build_schedule(
    bars: list[Bar],
    features: list[Optional[Feature]],
    start: str,
    end: str,
) -> tuple[dict[str, Threshold], list[dict[str, Any]]]:
    schedule: dict[str, Threshold] = {}
    audits: list[dict[str, Any]] = []
    for cutoff in month_sequence(start, end):
        candidate, evaluation, count, constraint = calibrate_month(
            bars, features, cutoff
        )
        month = cutoff.strftime("%Y-%m")
        schedule[month] = Threshold(
            month=month,
            entry_z30=candidate.entry_z30,
            entry_z60=candidate.entry_z60,
            exit_z30=candidate.exit_z30,
            exit_z60=candidate.exit_z60,
        )
        audits.append(
            {
                "month": month,
                "window_start": shift_months(
                    cutoff, -WINDOW_MONTHS
                ).isoformat(),
                "window_end": cutoff.fromordinal(
                    cutoff.toordinal() - 1
                ).isoformat(),
                **asdict(candidate),
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
            raise ValueError(
                f"缺少 {month} 阈值；应在月初重新校准，或显式使用 "
                "--allow-threshold-freeze"
            )
        eligible = [key for key in self.schedule if key <= month]
        if not eligible:
            raise ValueError(f"{month} 之前没有可冻结阈值")
        source_month = max(eligible)
        if month not in self.warned:
            eprint(f"警告: {month} 沿用 {source_month} 阈值")
            self.warned.add(month)
        source = self.schedule[source_month]
        return Threshold(
            month=month,
            entry_z30=source.entry_z30,
            entry_z60=source.entry_z60,
            exit_z30=source.exit_z30,
            exit_z60=source.exit_z60,
            source=f"frozen:{source_month}",
        )


def load_live_threshold_provider(
    path: Path,
    symbol: str,
    allow_freeze: bool,
) -> ThresholdProvider:
    """Load and validate an audited monthly publication for live inference."""
    raw = read_records(path)
    if not isinstance(raw, dict) or not isinstance(raw.get("threshold"), dict):
        raise ValueError(
            "live 的 --thresholds-file 必须是 calibrate 发布的月度JSON，"
            "不能直接使用研究回测阈值表"
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
    if raw.get("search_method") != SEARCH_METHOD:
        raise ValueError("阈值文件不是完整精确网格搜索结果")
    expected_evaluations = (
        len(ENTRY_Z30_GRID)
        * len(ENTRY_Z60_GRID)
        * len(EXIT_Z30_GRID)
        * len(EXIT_Z60_GRID)
    )
    if int(raw.get("search_evaluations", 0)) != expected_evaluations:
        raise ValueError(
            f"阈值文件搜索数不是完整网格 {expected_evaluations}"
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
    ) -> None:
        self.directory = directory
        self.symbol = symbol
        self.allow_freeze = allow_freeze
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
                f"缺少 {path}；应由月度定时任务先发布当月阈值"
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
        return Threshold(
            month=month,
            entry_z30=source.entry_z30,
            entry_z60=source.entry_z60,
            exit_z30=source.exit_z30,
            exit_z60=source.exit_z60,
            source=f"frozen:{source_month}",
        )


def in_range(key: str, start: str, end: str) -> bool:
    return start <= key[:10] <= end


def generate_actions(
    bars: list[Bar],
    features: list[Optional[Feature]],
    provider: ThresholdProvider,
    start: str,
    end: str,
) -> list[Action]:
    test_dates = sorted(
        {bar.trading_date for bar in bars if in_range(bar.key, start, end)}
    )
    session_index = {value: index for index, value in enumerate(test_dates)}
    actions: list[Action] = []
    state = 0
    pending: Optional[dict[str, str]] = None
    entry_session: Optional[int] = None
    for index, bar in enumerate(bars):
        if not in_range(bar.key, start, end):
            continue
        session = session_index[bar.trading_date]
        if pending is not None:
            actions.append(
                Action(
                    side=pending["side"],
                    signal_key=pending["signal_key"],
                    execution_key=bar.key,
                    execution_price=bar.open,
                    calibration_month=pending["calibration_month"],
                )
            )
            state = 1 if pending["side"] == "BUY" else 0
            entry_session = session if state else None
            pending = None
        feature = features[index]
        if feature is None or index == len(bars) - 1:
            continue
        threshold = provider.for_date(bar.trading_date)
        buy_score = (
            feature.z30 >= threshold.entry_z30
            and feature.z60 >= threshold.entry_z60
        )
        sell_score = (
            feature.z30 <= threshold.exit_z30
            and feature.z60 <= threshold.exit_z60
        )
        if state == 0 and bar.clock in ENTRY_TIMES and buy_score:
            pending = {
                "side": "BUY",
                "signal_key": bar.key,
                "calibration_month": threshold.month,
            }
        elif (
            state == 1
            and entry_session is not None
            and session > entry_session
            and bar.clock in EXIT_TIMES
            and sell_score
        ):
            pending = {
                "side": "SELL",
                "signal_key": bar.key,
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
        "window_months": WINDOW_MONTHS,
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
    schedule, audits = build_schedule(bars, features, start, end)
    provider = ThresholdProvider(schedule)
    actions = generate_actions(bars, features, provider, start, end)
    metrics, curve = simulate(bars, actions, start, end, args.cost_bps)
    metrics["bars_file"] = str(bars_path)
    if args.output_dir:
        write_outputs(
            Path(args.output_dir).resolve(),
            metrics,
            curve,
            actions,
            schedule,
            audits,
        )
    if args.json:
        print(json.dumps(metrics, ensure_ascii=False, indent=2))
    else:
        print(f"{VERSION} 回测完成")
        print(f"区间: {start} 至 {end}")
        print(f"策略收益: {metrics['total_return']:.4%}")
        print(f"基准收益: {metrics['benchmark_return']:.4%}")
        print(f"简单超额: {metrics['excess_difference']:.4%}")
        print(f"最大回撤: {metrics['max_drawdown']:.4%}")
        print(f"Sortino: {metrics['strategy_sortino']:.4f}")
    return 0


def live_connection(args: argparse.Namespace) -> tuple[str, int]:
    host = args.host
    port = args.port
    if args.config:
        config = configparser.ConfigParser()
        path = Path(args.config).resolve()
        if not config.read(path, encoding="utf-8"):
            raise ValueError(f"配置文件不存在或不可读: {path}")
        host = host or config.get(
            "CONFIG", "FUTU_HOST", fallback="127.0.0.1"
        )
        port = port or config.getint("CONFIG", "FUTU_PORT", fallback=11111)
    return host or "127.0.0.1", port or 11111


def run_fetch_bars(args: argparse.Namespace) -> int:
    """Refresh the rolling calibration input from Futu historical K-lines."""
    try:
        from futu import AuType, KLType, OpenQuoteContext, RET_OK
    except ImportError as exc:
        raise RuntimeError(
            "fetch-bars 需要 futu-api；安装命令: pip install futu-api"
        ) from exc

    cutoff = first_of_month(args.as_of)
    start = args.start or shift_months(
        cutoff, -(WINDOW_MONTHS + 1)
    ).isoformat()
    end = args.end or cutoff.fromordinal(cutoff.toordinal() - 1).isoformat()
    if start > end:
        raise ValueError(f"历史行情日期范围无效: {start} > {end}")

    host, port = live_connection(args)
    context = OpenQuoteContext(host=host, port=port)
    rows: list[dict[str, Any]] = []
    page_key = None
    try:
        while True:
            ret, data, page_key = context.request_history_kline(
                args.symbol,
                start=start,
                end=end,
                ktype=KLType.K_15M,
                autype=AuType.QFQ,
                max_count=1000,
                page_req_key=page_key,
            )
            if ret != RET_OK:
                raise RuntimeError(f"获取历史15分钟K线失败: {data}")
            for index in range(len(data)):
                row = data.iloc[index] if hasattr(data, "iloc") else data[index]
                rows.append(
                    {
                        "code": str(row.get("code", args.symbol)),
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
    finally:
        context.close()

    by_key = {row["time_key"]: row for row in rows if row["time_key"]}
    bars = [by_key[key] for key in sorted(by_key)]
    if not bars:
        raise ValueError(f"{args.symbol} 在 {start} 至 {end} 没有15分钟K线")
    payload = {
        "schema_version": 1,
        "source": "futu.request_history_kline",
        "symbol": args.symbol,
        "fetched_at": datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "start": start,
        "end": end,
        "bar_time_convention": args.futu_time_convention,
        "bars": bars,
    }
    output = Path(args.output).resolve()
    write_json_atomic(output, payload)
    print(
        json.dumps(
            {
                "output": str(output),
                "symbol": args.symbol,
                "start": start,
                "end": end,
                "bars": len(bars),
                "first_bar": bars[0]["time_key"],
                "last_bar": bars[-1]["time_key"],
            },
            ensure_ascii=False,
        )
    )
    return 0


def run_calibrate(args: argparse.Namespace) -> int:
    bars_path = Path(args.bars_file).resolve()
    bars = load_bars(
        bars_path,
        symbol=args.symbol,
        time_convention=args.bar_time_convention,
    )
    features = build_features(bars)
    cutoff = first_of_month(args.as_of)
    window_start = shift_months(cutoff, -WINDOW_MONTHS).isoformat()
    cutoff_text = cutoff.isoformat()
    training_bars = [
        bar
        for bar in bars
        if window_start <= bar.trading_date < cutoff_text
    ]
    candidate, evaluation, count, constraint = calibrate_month(
        bars, features, cutoff
    )
    threshold = Threshold(
        month=cutoff.strftime("%Y-%m"),
        entry_z30=candidate.entry_z30,
        entry_z60=candidate.entry_z60,
        exit_z30=candidate.exit_z30,
        exit_z60=candidate.exit_z60,
    )
    payload = {
        "schema_version": PUBLISH_SCHEMA_VERSION,
        "publication_kind": "monthly_threshold",
        "version": VERSION,
        "script_sha256": sha256_file(Path(__file__).resolve()),
        "published_at": datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "symbol": normalize_symbol(args.symbol),
        "month": cutoff.strftime("%Y-%m"),
        "available_from": cutoff.isoformat(),
        "window_start": window_start,
        "window_end": cutoff.fromordinal(cutoff.toordinal() - 1).isoformat(),
        "search_method": SEARCH_METHOD,
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
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    output = Path(args.output).resolve()
    write_json_atomic(output, payload)
    print(text, end="")
    return 0


class LiveState:
    def __init__(
        self,
        path: Path,
        initial_position: str,
        entry_date: Optional[str],
    ) -> None:
        self.path = path
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.position = int(raw.get("position", 0))
            self.entry_date = raw.get("entry_date")
            self.pending = raw.get("pending")
        else:
            self.position = 1 if initial_position == "long" else 0
            self.entry_date = entry_date if self.position else None
            self.pending = None
            if self.position and not self.entry_date:
                raise ValueError("--initial-position long 必须同时给 --entry-date")
            self.save()
        if self.position not in {0, 1}:
            raise ValueError("状态文件 position 只能是0或1")
        if self.position and not self.entry_date:
            raise ValueError("多头状态缺少 entry_date，无法执行T+1")

    def save(self) -> None:
        payload = {
            "version": VERSION,
            "position": self.position,
            "entry_date": self.entry_date,
            "pending": self.pending,
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
            name="m0-live-notifier",
            daemon=True,
        )
        self.thread.start()

    def should_notify(self, event: dict[str, Any]) -> bool:
        event_type = event.get("type")
        if event_type == "SIGNAL":
            return event.get("action") in {"BUY", "SELL"}
        if event_type == "ERROR":
            return True
        return self.notify_lifecycle and event_type in {"READY", "STOPPED"}

    def event_key(self, event: dict[str, Any]) -> str:
        return "|".join(
            str(event.get(key, ""))
            for key in ("type", "action", "bar_key", "message")
        )

    def notify(self, event: dict[str, Any]) -> None:
        if not self.should_notify(event):
            return
        now = time.monotonic()
        key = self.event_key(event)
        last = self.last_enqueued.get(key)
        if last is not None and now - last < 300:
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
        subject = f"M0 {symbol} {label}".strip()
        if event_type == "SIGNAL":
            feature = event.get("feature") or {}
            threshold = event.get("threshold") or {}
            message = (
                f"[M0] {symbol} {action}\n"
                f"信号K线: {event.get('bar_key')}\n"
                f"z30/z60: {float(feature.get('z30', math.nan)):.3f} / "
                f"{float(feature.get('z60', math.nan)):.3f}\n"
                f"阈值月份: {threshold.get('month', '')}\n"
                f"信号前仓位: {event.get('position_before')}"
            )
        elif event_type == "ERROR":
            message = f"[M0] {symbol} 实时推理错误\n{event.get('message', '')}"
        else:
            message = (
                f"[M0] {symbol} {event_type}\n"
                f"仓位状态: {event.get('position')}\n"
                f"时间: {event.get('emitted_at', '')}"
            )
        return subject, message

    def _run(self) -> None:
        while True:
            event = self.queue.get()
            try:
                if event is None:
                    return
                subject, message = self.format_event(event)
                for send in (
                    self.engine.send_webhook,
                    lambda text: self.engine.send_telegram_message(
                        text, "https://www.futunn.com/"
                    ),
                    lambda text: self.engine.send_email(subject, text),
                ):
                    try:
                        send(message)
                    except Exception as exc:
                        eprint(f"通知发送失败: {exc}")
            finally:
                self.queue.task_done()

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
        event_callback: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> None:
        self.symbol = symbol
        self.provider = provider
        self.state = state
        self.event_callback = event_callback
        self.shift_minutes = 15 if time_convention == "start" else 0
        self.completed: list[Bar] = []
        self.current: Optional[Bar] = None
        self.lock = threading.Lock()

    def emit(self, event: dict[str, Any]) -> None:
        payload = {
            "version": VERSION,
            "symbol": self.symbol,
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
                "active_threshold": asdict(
                    self.provider.for_date(self.current.trading_date)
                ),
            }
        )

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
        buy_score = (
            feature.z30 >= threshold.entry_z30
            and feature.z60 >= threshold.entry_z60
        )
        sell_score = (
            feature.z30 <= threshold.exit_z30
            and feature.z60 <= threshold.exit_z60
        )
        t1_sellable = bool(
            self.state.position
            and self.state.entry_date
            and bar.trading_date > self.state.entry_date
        )
        action = "NONE"
        if self.state.position == 0 and bar.clock in ENTRY_TIMES and buy_score:
            action = "BUY"
        elif (
            self.state.position == 1
            and t1_sellable
            and bar.clock in EXIT_TIMES
            and sell_score
        ):
            action = "SELL"
        if action != "NONE":
            self.state.pending = {
                "side": action,
                "signal_key": bar.key,
                "calibration_month": threshold.month,
            }
            self.state.save()
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
            }
        )

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
            self.execute_pending(bar)
            self.current = bar


def live_provider(args: argparse.Namespace) -> Any:
    if getattr(args, "thresholds_dir", None):
        return ThresholdDirectoryProvider(
            Path(args.thresholds_dir).resolve(),
            symbol=args.symbol,
            allow_freeze=args.allow_threshold_freeze,
        )
    return load_live_threshold_provider(
        Path(args.thresholds_file).resolve(),
        symbol=args.symbol,
        allow_freeze=args.allow_threshold_freeze,
    )


def _run_live(
    args: argparse.Namespace,
    notifier: Optional[LiveEventNotifier],
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
        )
    except ImportError as exc:
        raise RuntimeError(
            "live 模式需要 futu-api；安装命令: pip install futu-api"
        ) from exc

    provider = live_provider(args)
    state = LiveState(
        Path(args.state_file).resolve(),
        args.initial_position,
        args.entry_date,
    )
    engine = LiveSignalEngine(
        args.symbol,
        provider,
        state,
        args.futu_time_convention,
        event_callback=notifier.notify if notifier is not None else None,
    )

    class Handler(CurKlineHandlerBase):
        def on_recv_rsp(self, rsp_pb: Any) -> tuple[int, Any]:
            ret_code, data = super().on_recv_rsp(rsp_pb)
            if ret_code != RET_OK:
                engine.emit({"type": "ERROR", "message": str(data)})
                return RET_ERROR, data
            try:
                for index in range(len(data)):
                    row = data.iloc[index] if hasattr(data, "iloc") else data[index]
                    if str(row.get("code", "")) == args.symbol:
                        engine.on_bar(engine.bar_from_row(row))
            except Exception as exc:
                engine.emit({"type": "ERROR", "message": repr(exc)})
                return RET_ERROR, data
            return RET_OK, data

    host, port = live_connection(args)
    context = OpenQuoteContext(host=host, port=port)
    stopped = threading.Event()

    def stop_handler(_signum: int, _frame: Any) -> None:
        stopped.set()

    old_sigint = signal.signal(signal.SIGINT, stop_handler)
    old_sigterm = signal.signal(signal.SIGTERM, stop_handler)
    try:
        ret, message = context.subscribe(
            [args.symbol], [SubType.K_15M], subscribe_push=False
        )
        if ret != RET_OK:
            raise RuntimeError(f"订阅预热失败: {message}")
        ret, data = context.get_cur_kline(
            args.symbol,
            args.history_bars,
            KLType.K_15M,
            AuType.QFQ,
        )
        if ret != RET_OK:
            raise RuntimeError(f"获取预热K线失败: {data}")
        rows = [
            data.iloc[index] if hasattr(data, "iloc") else data[index]
            for index in range(len(data))
        ]
        engine.bootstrap(rows)
        context.set_handler(Handler())
        ret, message = context.subscribe(
            [args.symbol], [SubType.K_15M], subscribe_push=True
        )
        if ret != RET_OK:
            raise RuntimeError(f"开启K线推送失败: {message}")
        deadline = time.monotonic() + args.duration if args.duration > 0 else None
        while not stopped.wait(1.0):
            if deadline is not None and time.monotonic() >= deadline:
                break
    finally:
        signal.signal(signal.SIGINT, old_sigint)
        signal.signal(signal.SIGTERM, old_sigterm)
        context.close()
    engine.emit({"type": "STOPPED", "position": state.position})
    return 0


def run_live(args: argparse.Namespace) -> int:
    notifier = build_live_notifier(args.config, args.notify_lifecycle)
    try:
        return _run_live(args, notifier)
    except Exception as exc:
        if notifier is not None:
            notifier.notify(
                {
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


def add_bar_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--bars-file", required=True, help="15分钟OHLC JSON/CSV")
    parser.add_argument("--symbol", default="000902.SH")
    parser.add_argument(
        "--bar-time-convention",
        choices=("end", "start"),
        default="end",
        help="输入时间代表K线结束或开始；start自动加15分钟",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=f"{VERSION} 单文件校准、回测与信号推理"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    backtest = subparsers.add_parser(
        "backtest", help="自动rolling9校准并回测"
    )
    add_bar_arguments(backtest)
    backtest.add_argument("--start", required=True)
    backtest.add_argument("--end")
    backtest.add_argument("--cost-bps", type=float, default=5.0)
    backtest.add_argument("--output-dir")
    backtest.add_argument("--json", action="store_true")
    backtest.set_defaults(func=run_backtest)

    fetch_bars = subparsers.add_parser(
        "fetch-bars", help="从Futu刷新月度校准所需的15分钟历史K线"
    )
    fetch_bars.add_argument("--symbol", default="SH.000902")
    fetch_bars.add_argument("--as-of", required=True, help="目标月份首日")
    fetch_bars.add_argument("--start", help="可选覆盖历史起始日")
    fetch_bars.add_argument("--end", help="可选覆盖历史截止日")
    fetch_bars.add_argument("--output", required=True)
    fetch_bars.add_argument("--config", help="项目config.ini")
    fetch_bars.add_argument("--host", help="覆盖config.ini中的FUTU_HOST")
    fetch_bars.add_argument(
        "--port", type=int, help="覆盖config.ini中的FUTU_PORT"
    )
    fetch_bars.add_argument(
        "--futu-time-convention",
        choices=("end", "start"),
        default="start",
        help="记录Futu time_key口径，供后续calibrate使用",
    )
    fetch_bars.set_defaults(func=run_fetch_bars)

    calibrate = subparsers.add_parser(
        "calibrate", help="仅使用目标月份以前数据生成当月阈值"
    )
    add_bar_arguments(calibrate)
    calibrate.add_argument("--as-of", required=True)
    calibrate.add_argument(
        "--output",
        required=True,
        help="原子发布的月度阈值JSON路径",
    )
    calibrate.set_defaults(func=run_calibrate)

    live = subparsers.add_parser(
        "live", help="连接Futu OpenD并输出信号；不下单"
    )
    live.add_argument("--symbol", required=True, help="例如 SH.000902")
    live.add_argument(
        "--config",
        help="项目config.ini；复用Futu连接与Telegram/Email/Webhook配置",
    )
    live.add_argument("--host", help="覆盖config.ini中的FUTU_HOST")
    live.add_argument(
        "--port", type=int, help="覆盖config.ini中的FUTU_PORT"
    )
    live.add_argument("--history-bars", type=int, default=200)
    live.add_argument("--duration", type=int, default=0)
    live.add_argument("--state-file", required=True)
    live.add_argument(
        "--initial-position", choices=("flat", "long"), default="flat"
    )
    live.add_argument("--entry-date")
    live.add_argument(
        "--futu-time-convention",
        choices=("end", "start"),
        required=True,
        help="必须按已核对的Futu time_key口径显式指定",
    )
    threshold_source = live.add_mutually_exclusive_group(required=True)
    threshold_source.add_argument(
        "--thresholds-file",
        help="由 calibrate 发布并通过审计校验的当月JSON",
    )
    threshold_source.add_argument(
        "--thresholds-dir",
        help="按threshold_YYYY-MM.json命名的发布目录；跨月自动热加载",
    )
    live.add_argument(
        "--allow-threshold-freeze",
        action="store_true",
        help="仅作显式运营降级：当前月缺失时沿用最近发布阈值",
    )
    live.add_argument(
        "--notify-lifecycle",
        action="store_true",
        help="除交易信号和错误外，也通知READY/STOPPED",
    )
    live.set_defaults(func=run_live)
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
