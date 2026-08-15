"""原生动量轮动回测、优化与 Futu 实时信号监控。

live 示例（每个交易日收盘后检测并通知一次；不下单）：
    python market_analysis/momentum_rotation_strategy.py live-us \
        --runtime-dir /absolute/path/runtime --config config.ini
    python market_analysis/momentum_rotation_strategy.py live-cn \
        --runtime-dir /absolute/path/runtime --config config.ini
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import math
import os
import queue
import signal
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime, time as datetime_time
from itertools import combinations
from pathlib import Path
from time import perf_counter
from typing import Any, Iterator, Optional
from zoneinfo import ZoneInfo

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np
from tools import calc_momentum


def calculate_momentum_score(data: np.ndarray) -> float:
    if len(data) < 2 or np.any(data <= 0):
        return float("-inf")
    return float(
        calc_momentum(
            pd.Series(data, dtype=float), N=len(data), method="linear"
        ).iloc[-1]
    )


# Latest Futu/QFQ native backtests for the two live presets:
# US.QQQ US.SPY US.FXI US.UUP, window=21, 2016-06-14..2026-06-11,
# total=521.47%, max_dd=-28.28%; benchmark US.SPY total=315.69%, max_dd=-33.72%.
# SZ.159941 SZ.159949 SH.510300 SH.510880, window=28, 2016-07-25..2026-08-14,
# total=368.75%, max_dd=-195.59%; benchmark SH.510300 total=71.82%, max_dd=-42.16%.
# The CN equity curve crossed below zero; its later total return is not economically viable.
# US.UUP is a cash signal: when it ranks first, live-us holds cash.

LIVE_STRATEGY = "momentum-rotation"
LIVE_VERSION = "momentum-rotation-live-v2"
LIVE_SCHEMA_VERSION = 1
US_MARKET_TIMEZONE = ZoneInfo("America/New_York")
CN_MARKET_TIMEZONE = ZoneInfo("Asia/Shanghai")
# Covers US 13:00 early closes while still rejecting a quote stale since noon.
LIVE_MAX_QUOTE_AGE_SECONDS = 4 * 60 * 60
LIVE_MAX_CONSECUTIVE_ERRORS = 5

DEFAULT_MODE = "optimize-universe"
DEFAULT_START = "2016-06-12"
DEFAULT_END = date.today().isoformat()
DEFAULT_CAPITAL = 1_000_000
DEFAULT_OPTIMIZATION_TARGET = "sortino_ratio"
DEFAULT_CANDIDATE_SYMBOLS = ["US.QQQ", "US.GLD", "US.SPY", "US.UUP", "US.FXI", "US.TLT"]
# DEFAULT_CANDIDATE_SYMBOLS = ["SZ.159941", "SH.518880", "SZ.159949", "SH.510300", "SH.510880"]
LIVE_PRESETS: dict[str, dict[str, Any]] = {
    "live-us": {
        "pairs": (
            ("US.QQQ", 21),
            ("US.SPY", 21),
            ("US.FXI", 21),
            ("US.UUP", 21),
        ),
        "cash_symbols": {"US.UUP"},
        "timezone": US_MARKET_TIMEZONE,
        "notification_time": datetime_time(16, 10),
    },
    "live-cn": {
        "pairs": (
            ("SZ.159941", 28),
            ("SZ.159949", 28),
            ("SH.510300", 28),
            ("SH.510880", 28),
        ),
        "cash_symbols": set(),
        "timezone": CN_MARKET_TIMEZONE,
        "notification_time": datetime_time(15, 10),
    },
}
DEFAULT_LIVE_ETF_WINDOWS = LIVE_PRESETS["live-us"]["pairs"]
DEFAULT_BACKTEST_SYMBOLS = [symbol for symbol, _window in DEFAULT_LIVE_ETF_WINDOWS]
DEFAULT_UNIVERSE_SIZE = 4
DEFAULT_BENCHMARK_SYMBOL = "US.SPY"
DEFAULT_CASH_SYMBOLS = ["US.UUP"]
OPTIMIZATION_WINDOWS = range(20, 31)
HISTORY_CACHE_DIR = PROJECT_ROOT / "data" / "backtest_cache" / "futu" / "source"
OUTPUT_ROOT = PROJECT_ROOT / "output" / "backtest"


@dataclass(frozen=True)
class BacktestConfig:
    symbols: list[str]
    start: str
    end: str
    config_path: Optional[str] = None
    window: int = 21
    capital: int = DEFAULT_CAPITAL
    rate: float = 0.001
    # 每份固定单边价格差：美股 ETF 基准 $0.01；A股 ETF 基准 ¥0.001。
    slippage: float = 0.01
    size: int = 1
    pricetick: float = 0.001
    annual_days: int = 250


def make_backtest_config(
    args: argparse.Namespace,
    symbols: list[str],
) -> BacktestConfig:
    return BacktestConfig(
        symbols=symbols,
        start=args.start,
        end=args.end,
        config_path=args.config,
    )


def _normalize_ohlcv(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    frame.index = pd.to_datetime(frame.index).tz_localize(None).normalize()
    frame = frame.rename(columns={name: str(name).strip().title() for name in frame})
    required = ["Open", "High", "Low", "Close", "Volume"]
    missing = [name for name in required if name not in frame]
    if missing:
        raise ValueError(f"Missing OHLCV columns: {missing}")
    frame = frame[required].apply(pd.to_numeric, errors="coerce").dropna()
    return frame[~frame.index.duplicated(keep="last")].sort_index()


def _configured_source(config: configparser.ConfigParser, symbol: str) -> str:
    market_key = f"DATA_SOURCE_{symbol.split('.', 1)[0].upper()}"
    return config.get(
        "CONFIG",
        market_key,
        fallback=config.get("CONFIG", "DATA_SOURCE", fallback=""),
    ).strip().lower()


def _futu_config(path: Optional[str], symbol: str) -> configparser.ConfigParser:
    candidates = (
        [Path(path)]
        if path
        else [PROJECT_ROOT / "config.ini", PROJECT_ROOT / "config_template.ini"]
    )
    for candidate in candidates:
        candidate = candidate if candidate.is_absolute() else PROJECT_ROOT / candidate
        if candidate.exists():
            config = configparser.ConfigParser()
            if not config.read(candidate, encoding="utf-8") or not config.has_section(
                "CONFIG"
            ):
                raise ValueError(f"配置文件不可读或缺少 CONFIG: {candidate}")
            source = _configured_source(config, symbol)
            if source != "futu":
                raise ValueError(
                    f"{symbol} 的 DATA_SOURCE 必须在配置文件中设为 futu，当前为 {source or '空'}"
                )
            config.set("CONFIG", "FUTU_PUSH_TYPE", "K_DAY")
            return config
    raise FileNotFoundError(f"Cannot find config file from: {candidates}")


def prepare_history(config: BacktestConfig) -> dict[str, pd.DataFrame]:
    from data import get_kline_data

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    start, end = pd.Timestamp(config.start), pd.Timestamp(config.end)
    count = max(len(pd.bdate_range(start, pd.Timestamp.today())) + 30, 270)
    histories = {}
    symbols = list(dict.fromkeys(config.symbols + [DEFAULT_BENCHMARK_SYMBOL]))
    for symbol in symbols:
        frame = get_kline_data(
            symbol,
            _futu_config(config.config_path, symbol),
            max_count=count,
            file_cache_dir=str(HISTORY_CACHE_DIR),
        )
        if frame is None or frame.empty:
            raise RuntimeError(f"No Futu data for {symbol}")
        frame = _normalize_ohlcv(frame).loc[start:end]
        if frame.empty:
            raise RuntimeError(f"No history data for {symbol} in {config.start} to {config.end}")
        histories[symbol] = frame
    return histories


def _fill_price(side: str, limit: float, bar: pd.Series) -> Optional[float]:
    if side == "long" and limit >= bar["Low"] > 0:
        return min(limit, float(bar["Open"]))
    if side == "short" and limit <= bar["High"] and bar["High"] > 0:
        return max(limit, float(bar["Open"]))
    return None


def _simulate_momentum(
    config: BacktestConfig,
    histories: Optional[dict[str, pd.DataFrame]] = None,
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    loaded = histories or prepare_history(config)
    histories = {symbol: loaded[symbol] for symbol in config.symbols}
    dates = sorted(set().union(*(frame.index for frame in histories.values())))
    if len(dates) < 2:
        raise RuntimeError("Backtest requires at least two bars")

    warmup = max(config.window + 5, 10)
    buffers = {symbol: [] for symbol in config.symbols}
    positions = {symbol: 0 for symbol in config.symbols}
    targets = positions.copy()
    last_prices: dict[str, float] = {}
    last_closes: dict[str, float] = {}
    pre_closes = {symbol: float(frame.iloc[0]["Close"]) for symbol, frame in histories.items()}
    sizing_cash = float(config.capital)
    orders: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    daily: list[dict[str, Any]] = []

    for index, raw_date in enumerate(dates):
        current = pd.Timestamp(raw_date).normalize()
        bars = {symbol: frame.loc[current] for symbol, frame in histories.items() if current in frame.index}
        start_positions = positions.copy()
        day_trades: list[dict[str, Any]] = []

        if index:
            remaining = []
            for order in orders:
                bar = bars.get(order["vt_symbol"])
                price = None if bar is None else _fill_price(order["direction"], order["price"], bar)
                if price is None:
                    remaining.append(order)
                    continue
                change = order["volume"] if order["direction"] == "long" else -order["volume"]
                positions[order["vt_symbol"]] += change
                sizing_cash -= change * price * config.size
                trade = {"datetime": current, **order, "price": price}
                trades.append(trade)
                day_trades.append(trade)
            orders = remaining

        for symbol, bar in bars.items():
            price = float(bar["Close"])
            last_prices[symbol] = price
            last_closes[symbol] = price
            buffers[symbol].append(price)
            buffers[symbol] = buffers[symbol][-warmup:]

        if all(len(buffer) == warmup for buffer in buffers.values()):
            scores = {
                symbol: calculate_momentum_score(np.asarray(buffer[-config.window:]))
                for symbol, buffer in buffers.items()
            }
            selected = max(scores, key=scores.get)
            new_targets = {symbol: 0 for symbol in config.symbols}
            if selected not in DEFAULT_CASH_SYMBOLS:
                value = sizing_cash + sum(
                    positions[symbol] * last_prices[symbol] * config.size
                    for symbol in config.symbols
                )
                new_targets[selected] = max(
                    int(max(value, 0) / (last_prices[selected] * config.size)),
                    0,
                )
            if new_targets != targets:
                targets = new_targets
                orders = []
                for symbol, bar in bars.items():
                    change = targets[symbol] - positions[symbol]
                    if not change:
                        continue
                    side = "long" if change > 0 else "short"
                    orders.append(
                        {
                            "vt_symbol": symbol,
                            "direction": side,
                            "offset": "close" if positions[symbol] * change < 0 else "open",
                            "price": round(float(bar["Close"]) / config.pricetick) * config.pricetick,
                            "volume": abs(change),
                        }
                    )

        if not index:
            continue
        holding_pnl = sum(
            start_positions[symbol] * (close - pre_closes[symbol]) * config.size
            for symbol, close in last_closes.items()
        )
        trading_pnl = sum(
            (trade["volume"] if trade["direction"] == "long" else -trade["volume"])
            * (last_closes[trade["vt_symbol"]] - trade["price"])
            * config.size
            for trade in day_trades
        )
        turnover = sum(trade["volume"] * trade["price"] * config.size for trade in day_trades)
        slippage = sum(trade["volume"] * config.slippage * config.size for trade in day_trades)
        commission = turnover * config.rate
        total_pnl = holding_pnl + trading_pnl
        daily.append(
            {
                "date": current,
                "trade_count": len(day_trades),
                "turnover": turnover,
                "commission": commission,
                "slippage": slippage,
                "trading_pnl": trading_pnl,
                "holding_pnl": holding_pnl,
                "total_pnl": total_pnl,
                "net_pnl": total_pnl - commission - slippage,
            }
        )
        pre_closes.update(last_closes)

    frame = pd.DataFrame(daily).set_index("date")
    frame["balance"] = config.capital + frame["net_pnl"].cumsum()
    frame["return"] = np.log(frame["balance"] / frame["balance"].shift()).fillna(0)
    frame["highlevel"] = frame["balance"].cummax()
    frame["drawdown"] = frame["balance"] - frame["highlevel"]
    frame["ddpercent"] = frame["drawdown"] / frame["highlevel"] * 100
    return frame, trades


def _drawdown(equity: pd.Series) -> tuple[pd.Series, pd.Series]:
    drawdown = equity - equity.cummax()
    return drawdown, drawdown / equity.cummax() * 100


def _statistics(
    frame: pd.DataFrame,
    config: BacktestConfig,
    histories: dict[str, pd.DataFrame],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    days = len(frame)
    end_balance = float(frame["balance"].iloc[-1])
    total_return = (end_balance / config.capital - 1) * 100
    annual_return = total_return / days * config.annual_days
    return_std = float(frame["return"].std() * 100)
    daily_return = float(frame["return"].mean() * 100)
    max_ddpercent = float(frame["ddpercent"].min())
    downside = frame["balance"].pct_change().fillna(0).clip(upper=0)
    downside_deviation = math.sqrt(float(downside.pow(2).mean())) * math.sqrt(config.annual_days)
    statistics: dict[str, Any] = {
        "start_date": frame.index[0],
        "end_date": frame.index[-1],
        "total_days": days,
        "profit_days": int((frame["net_pnl"] > 0).sum()),
        "loss_days": int((frame["net_pnl"] < 0).sum()),
        "capital": config.capital,
        "end_balance": end_balance,
        "max_drawdown": float(frame["drawdown"].min()),
        "max_ddpercent": max_ddpercent,
        "total_net_pnl": float(frame["net_pnl"].sum()),
        "total_commission": float(frame["commission"].sum()),
        "total_slippage": float(frame["slippage"].sum()),
        "total_turnover": float(frame["turnover"].sum()),
        "total_trade_count": int(frame["trade_count"].sum()),
        "total_return": total_return,
        "annual_return": annual_return,
        "daily_return": daily_return,
        "return_std": return_std,
        "sharpe_ratio": daily_return / return_std * math.sqrt(config.annual_days) if return_std else 0,
        "sortino_ratio": annual_return / 100 / downside_deviation if downside_deviation else 0,
        "calmar_ratio": annual_return / abs(max_ddpercent) if max_ddpercent else 0,
        "annual_downside_deviation": downside_deviation * 100,
    }

    dates = pd.DatetimeIndex(frame.index).normalize()
    benchmark_close = histories[DEFAULT_BENCHMARK_SYMBOL]["Close"].reindex(dates).ffill()
    benchmark_equity = benchmark_close / benchmark_close.dropna().iloc[0] * config.capital
    benchmark_return = benchmark_equity.pct_change().fillna(0) * 100
    strategy_equity = pd.Series(frame["balance"].to_numpy(), index=dates)
    excess_return = strategy_equity.pct_change().fillna(0) * 100 - benchmark_return
    excess_equity = (1 + excess_return / 100).cumprod() * config.capital
    excess_drawdown, excess_ddpercent = _drawdown(excess_equity)
    frame["benchmark_symbol"] = DEFAULT_BENCHMARK_SYMBOL
    frame["benchmark_close"] = benchmark_close.to_numpy()
    frame["benchmark_balance"] = benchmark_equity.to_numpy()
    frame["benchmark_return"] = benchmark_return.to_numpy()
    frame["excess_return"] = excess_return.to_numpy()
    frame["excess_balance"] = excess_equity.to_numpy()
    frame["excess_drawdown"] = excess_drawdown.to_numpy()
    frame["excess_ddpercent"] = excess_ddpercent.to_numpy()
    span = max((dates[-1] - dates[0]).days, 1)
    annual_factor = 365 / span
    benchmark_total = (benchmark_equity.iloc[-1] / benchmark_equity.iloc[0] - 1) * 100
    benchmark_annual = ((benchmark_equity.iloc[-1] / benchmark_equity.iloc[0]) ** annual_factor - 1) * 100
    benchmark_dd = float(_drawdown(benchmark_equity)[1].min())
    benchmark_downside = (benchmark_return / 100).clip(upper=0)
    benchmark_downside_deviation = math.sqrt(float(benchmark_downside.pow(2).mean())) * math.sqrt(config.annual_days)
    statistics.update(
        {
            "benchmark_symbol": DEFAULT_BENCHMARK_SYMBOL,
            "benchmark_total_return": benchmark_total,
            "benchmark_annual_return": benchmark_annual,
            "benchmark_max_ddpercent": benchmark_dd,
            "benchmark_sortino_ratio": benchmark_annual / 100 / benchmark_downside_deviation if benchmark_downside_deviation else 0,
            "benchmark_calmar_ratio": benchmark_annual / abs(benchmark_dd) if benchmark_dd else 0,
            "excess_total_return": (strategy_equity.iloc[-1] / strategy_equity.iloc[0] - benchmark_equity.iloc[-1] / benchmark_equity.iloc[0]) * 100,
            "excess_annual_return": ((strategy_equity.iloc[-1] / strategy_equity.iloc[0]) ** annual_factor - (benchmark_equity.iloc[-1] / benchmark_equity.iloc[0]) ** annual_factor) * 100,
            "excess_max_ddpercent": float(excess_ddpercent.min()),
            "tracking_error": float(excess_return.std() * math.sqrt(config.annual_days)),
            "information_ratio": float(excess_return.mean() / excess_return.std() * math.sqrt(config.annual_days)) if excess_return.std() else 0,
        }
    )
    return frame, statistics


def _result_row(config: BacktestConfig, statistics: dict[str, Any]) -> dict[str, Any]:
    return {
        "target": statistics[DEFAULT_OPTIMIZATION_TARGET],
        "setting": str({"regression_window": config.window}),
        "symbols": " ".join(config.symbols),
        "param_regression_window": config.window,
        **statistics,
    }


def _print_results(rows: list[dict[str, Any]], title: str, limit: int = 3) -> None:
    print(title, flush=True)
    for index, row in enumerate(rows[:limit], 1):
        print(f"{index}. {row['symbols']}", flush=True)
        print(f"   window={row['param_regression_window']}, total={row['total_return']:.2f}%, annual={row['annual_return']:.2f}%", flush=True)
        print(f"   max_dd={row['max_ddpercent']:.2f}%, Sharpe={row['sharpe_ratio']:.4f}, Sortino={row['sortino_ratio']:.4f}, Calmar={row['calmar_ratio']:.4f}", flush=True)
        print(f"   benchmark={row['benchmark_symbol']}, benchmark_total={row['benchmark_total_return']:.2f}%, excess_total={row['excess_total_return']:.2f}%\n", flush=True)


def run_momentum_backtest(config: BacktestConfig) -> dict[str, Any]:
    histories = prepare_history(config)
    frame, trades = _simulate_momentum(config, histories)
    frame, statistics = _statistics(frame, config, histories)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    daily_path = OUTPUT_ROOT / f"momentumrotationstrategy_daily_{stamp}.csv"
    trades_path = OUTPUT_ROOT / f"momentumrotationstrategy_trades_{stamp}.csv"
    frame.to_csv(daily_path)
    pd.DataFrame(trades).to_csv(trades_path, index=False)
    print(f"Daily result: {daily_path}\nTrades:       {trades_path}", flush=True)
    _print_results([_result_row(config, statistics)], "Backtest result:", 1)
    return statistics


def run_momentum_optimization(
    config: BacktestConfig,
    histories: Optional[dict[str, pd.DataFrame]] = None,
) -> list[dict[str, Any]]:
    histories = histories or prepare_history(config)
    started = perf_counter()
    rows = []
    for window in OPTIMIZATION_WINDOWS:
        trial = replace(config, window=window)
        frame, _ = _simulate_momentum(trial, histories)
        _, statistics = _statistics(frame, trial, histories)
        rows.append(_result_row(trial, statistics))
    rows.sort(key=lambda row: row["target"], reverse=True)
    path = OUTPUT_ROOT / f"momentumrotationstrategy_optimization_{datetime.now():%Y%m%d_%H%M%S_%f}.csv"
    pd.DataFrame(rows).to_csv(path, index=False)
    print(f"Optimization result: {path} (elapsed={format_elapsed(perf_counter() - started)})", flush=True)
    _print_results(rows, "Top 3 optimization results:")
    return rows


def format_elapsed(seconds: float) -> str:
    total_seconds = max(int(seconds), 0)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{seconds:02d}s"
    if minutes:
        return f"{minutes}m{seconds:02d}s"
    return f"{seconds}s"


def run_universe_optimization(args: argparse.Namespace) -> list[dict[str, Any]]:
    candidates = DEFAULT_CANDIDATE_SYMBOLS
    rows: list[dict[str, Any]] = []
    combos = list(combinations(candidates, DEFAULT_UNIVERSE_SIZE))
    started_at = perf_counter()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    progress_path = OUTPUT_ROOT / f"momentumrotationstrategy_universe_progress_{stamp}.csv"

    print(
        "Universe optimization started: "
        f"candidates={len(candidates)}, choose={DEFAULT_UNIVERSE_SIZE}, "
        f"combos={len(combos)}, target={DEFAULT_OPTIMIZATION_TARGET}, "
        f"progress={progress_path}",
        flush=True,
    )

    print(f"Preparing history data for candidate universe: {' '.join(candidates)}", flush=True)
    histories = prepare_history(make_backtest_config(args, candidates))

    for index, combo in enumerate(combos, start=1):
        symbols = list(combo)
        combo_started_at = perf_counter()
        print(f"Universe {index}/{len(combos)} started: {' '.join(symbols)}", flush=True)
        row = run_momentum_optimization(make_backtest_config(args, symbols), histories)[0]
        rows.append(row)
        pd.DataFrame(rows).sort_values("target", ascending=False).to_csv(progress_path, index=False)
        print(
            f"Universe {index}/{len(combos)} done: "
            f"target={row['target']:.6f}, elapsed={format_elapsed(perf_counter() - combo_started_at)}, "
            f"total_elapsed={format_elapsed(perf_counter() - started_at)}",
            flush=True,
        )

    path = OUTPUT_ROOT / f"momentumrotationstrategy_universe_optimization_{stamp}.csv"
    rows.sort(key=lambda row: row["target"], reverse=True)
    pd.DataFrame(rows).to_csv(path, index=False)
    print(
        f"Universe optimization result: {path} "
        f"(elapsed={format_elapsed(perf_counter() - started_at)}, progress={progress_path})",
        flush=True,
    )
    _print_results(rows, "Top 3 universe optimization results:")
    return rows


@dataclass(frozen=True)
class LiveRuntimePaths:
    root: Path
    state_file: Path
    lock_file: Path

    @classmethod
    def from_argument(cls, raw: str, mode: str) -> "LiveRuntimePaths":
        root = Path(raw).expanduser()
        if not root.is_absolute():
            raise ValueError("--runtime-dir 必须使用绝对路径")
        root = Path(os.path.abspath(root))
        return cls(root, root / f"state-{mode}.json", root / f"{mode}.lock")

    def prepare(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)


class RuntimeFileLock:
    """Prevent two live processes from sharing one state directory."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Any = None

    def __enter__(self) -> "RuntimeFileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0, os.SEEK_END)
                if self.handle.tell() == 0:
                    self.handle.write(b"\0")
                    self.handle.flush()
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(
                    self.handle.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
        except (OSError, BlockingIOError) as exc:
            self.handle.close()
            self.handle = None
            raise RuntimeError(
                f"已有 live 实例占用运行目录: {self.path.parent}"
            ) from exc
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        if self.handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


def eprint(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def score_live_pairs(
    pairs: list[tuple[str, int]],
    closes: dict[str, list[float] | np.ndarray],
) -> dict[str, float]:
    scores: dict[str, float] = {}
    for symbol, window in pairs:
        values = np.asarray(closes.get(symbol, ()), dtype=float)
        if len(values) < window:
            raise RuntimeError(
                f"{symbol} K线不足: 需要 {window} 根，实际 {len(values)} 根"
            )
        score = calculate_momentum_score(values[-window:])
        if not math.isfinite(score):
            raise RuntimeError(f"{symbol} 无法计算有效动量分数")
        scores[symbol] = score
    return scores


def select_live_symbol(
    scores: dict[str, float],
    min_score: float,
) -> Optional[str]:
    if not scores:
        return None
    symbol = max(scores, key=scores.get)
    return symbol if scores[symbol] >= min_score else None


def live_transition_action(
    previous_symbol: Optional[str],
    selected_symbol: Optional[str],
    cash_symbols: set[str],
) -> str:
    if previous_symbol == selected_symbol:
        return "NONE"
    if previous_symbol is None:
        return "INITIAL"
    if selected_symbol is None or selected_symbol in cash_symbols:
        return "SELL"
    if previous_symbol in cash_symbols:
        return "BUY"
    return "ROTATE"


class LiveState:
    def __init__(self, path: Path, pairs: list[tuple[str, int]]) -> None:
        self.path = path
        expected_pairs = [
            {"symbol": symbol, "window": window} for symbol, window in pairs
        ]
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if (
                payload.get("strategy") != LIVE_STRATEGY
                or payload.get("pairs") != expected_pairs
            ):
                raise ValueError("实时状态文件与当前 ETF/窗口配置不匹配")
            self.selected_symbol = payload.get("selected_symbol")
            self.last_evaluation_date = payload.get("last_evaluation_date")
            self.last_snapshot = payload.get("last_snapshot")
            self.last_error = payload.get("last_error")
        else:
            self.selected_symbol = None
            self.last_evaluation_date = None
            self.last_snapshot = None
            self.last_error = None
            self.save(pairs)

    def save(self, pairs: list[tuple[str, int]]) -> None:
        write_json_atomic(
            self.path,
            {
                "schema_version": LIVE_SCHEMA_VERSION,
                "strategy": LIVE_STRATEGY,
                "version": LIVE_VERSION,
                "pairs": [
                    {"symbol": symbol, "window": window}
                    for symbol, window in pairs
                ],
                "selected_symbol": self.selected_symbol,
                "last_evaluation_date": self.last_evaluation_date,
                "updated_at": datetime.now().astimezone().isoformat(
                    timespec="seconds"
                ),
                "last_snapshot": self.last_snapshot,
                "last_error": self.last_error,
            },
        )

    def record_snapshot(
        self,
        pairs: list[tuple[str, int]],
        event: dict[str, Any],
    ) -> None:
        self.last_snapshot = event
        self.last_error = None
        self.save(pairs)

    def record_error(
        self,
        pairs: list[tuple[str, int]],
        event: dict[str, Any],
    ) -> None:
        self.last_error = event
        self.save(pairs)


class LiveNotifier:
    """Send notifications outside the market-data polling thread."""

    def __init__(self, engine: Any, maxsize: int = 100) -> None:
        self.engine = engine
        self.recent: dict[str, float] = {}
        self.queue: queue.Queue[Optional[dict[str, Any]]] = queue.Queue(
            maxsize=maxsize
        )
        self.thread = threading.Thread(
            target=self._run,
            name="momentum-rotation-notifier",
            daemon=True,
        )
        self.thread.start()

    def notify(self, event: dict[str, Any]) -> None:
        key = "|".join(
            str(event.get(name, ""))
            for name in ("type", "action", "selected_symbol", "message")
        )
        now = time.monotonic()
        if now - self.recent.get(key, -1e12) < 300:
            return
        self.recent[key] = now
        try:
            self.queue.put_nowait(dict(event))
        except queue.Full:
            eprint(f"通知队列已满，丢弃事件: {key}")

    @staticmethod
    def format_event(event: dict[str, Any]) -> tuple[str, str]:
        action = event.get("action") or event["type"]
        subject = f"动量轮动 {action}"
        if event["type"] != "SIGNAL":
            return subject, f"动量轮动 {event['type']}\n{event.get('message', '')}"

        target = event.get("target_symbol") or "CASH"
        lines = [
            f"动量轮动 {event['action']}",
            f"模式: {event['mode']}",
            f"交易日: {event['evaluation_date']}",
            f"提示时间: {event['notification_time']} {event['timezone']}",
            f"目标: {target}",
            f"排名首位: {event.get('selected_symbol') or '无'}",
            "动量排名:",
        ]
        for row in event["ranking"]:
            lines.append(
                f"- {row['symbol']} window={row['window']}: "
                f"{row['score']:.2%}"
            )
        return subject, "\n".join(lines)

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
            except Exception as exc:
                eprint(f"通知处理失败: {exc}")
            finally:
                self.queue.task_done()

    def close(self, timeout: float = 10.0) -> None:
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            eprint("通知队列未清空，退出时无法等待全部通知")
            return
        self.thread.join(timeout=timeout)


def build_live_notifier(config_path: Optional[str]) -> Optional[LiveNotifier]:
    if not config_path:
        return None
    config = configparser.ConfigParser()
    path = Path(config_path).expanduser().resolve()
    if not config.read(path, encoding="utf-8"):
        raise ValueError("配置文件不存在或不可读")
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from notification_engine import NotificationEngine

    return LiveNotifier(NotificationEngine(config))


def resolve_live_connection(
    config_path: Optional[str],
    symbols: list[str],
) -> tuple[str, int]:
    if not config_path:
        raise ValueError("live 模式必须指定 --config")
    config = _futu_config(config_path, symbols[0])
    for symbol in symbols[1:]:
        if _configured_source(config, symbol) != "futu":
            raise ValueError(f"{symbol} 的 DATA_SOURCE 必须在配置文件中设为 futu")
    host = config.get("CONFIG", "FUTU_HOST", fallback="").strip()
    port = config.getint("CONFIG", "FUTU_PORT", fallback=0)
    if not host or not 1 <= port <= 65535:
        raise ValueError("配置文件必须提供有效的 FUTU_HOST 和 FUTU_PORT")
    return host, port


@contextmanager
def stop_event() -> Iterator[threading.Event]:
    stopped = threading.Event()
    old_handlers = {
        sig: signal.signal(sig, lambda *_: stopped.set())
        for sig in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        yield stopped
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)


def daily_evaluation_ready(
    now: datetime,
    notification_time: datetime_time,
    market_timezone: ZoneInfo,
) -> bool:
    """Return true after the configured market close buffer on weekdays."""
    local = now.astimezone(market_timezone)
    if local.weekday() >= 5:
        return False
    return local.time().replace(tzinfo=None) >= notification_time


def parse_futu_quote_time(
    value: Any,
    market_timezone: ZoneInfo,
) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text or text in {"N/A", "None"}:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=market_timezone)
    return parsed.astimezone(market_timezone)


def subscribe_live_klines(
    context: Any,
    symbols: list[str],
    ret_ok: Any,
    subtype: Any,
) -> None:
    ret, data = context.subscribe(
        symbols,
        [subtype.K_DAY],
        is_first_push=False,
    )
    if ret != ret_ok:
        raise RuntimeError(f"Futu 日K订阅失败: {data}")


def is_live_trading_day(
    context: Any,
    symbol: str,
    trading_date: str,
    ret_ok: Any,
) -> bool:
    ret, days = context.request_trading_days(
        start=trading_date,
        end=trading_date,
        code=symbol,
    )
    if ret != ret_ok:
        raise RuntimeError(f"Futu 交易日查询失败: {days}")
    return any(str(day.get("time", ""))[:10] == trading_date for day in days)


def fetch_live_market_data(
    context: Any,
    pairs: list[tuple[str, int]],
    ret_ok: Any,
    kl_type: Any,
    au_type: Any,
) -> tuple[dict[str, list[float]], dict[str, dict[str, Any]]]:
    symbols = [symbol for symbol, _window in pairs]
    ret, snapshots = context.get_market_snapshot(symbols)
    if ret != ret_ok or snapshots.empty:
        raise RuntimeError(f"Futu 快照失败: {snapshots}")

    snapshot_by_symbol: dict[str, dict[str, Any]] = {}
    for _, row in snapshots.iterrows():
        symbol = str(row.get("code", "")).upper()
        price = float(row.get("last_price", 0))
        if symbol and math.isfinite(price) and price > 0:
            snapshot_by_symbol[symbol] = {
                "price": price,
                "quote_time": str(row.get("update_time", "") or ""),
            }

    closes: dict[str, list[float]] = {}
    for symbol, window in pairs:
        snapshot = snapshot_by_symbol.get(symbol)
        if snapshot is None:
            raise RuntimeError(f"Futu 快照缺少 {symbol} 或价格无效")
        ret, frame = context.get_cur_kline(
            symbol,
            window,
            kl_type.K_DAY,
            au_type.QFQ,
        )
        if ret != ret_ok or frame.empty:
            raise RuntimeError(f"Futu 日K失败 {symbol}: {frame}")
        if "time_key" not in frame.columns:
            raise RuntimeError(f"Futu 日K缺少 time_key: {symbol}")
        bar_date = str(frame["time_key"].iloc[-1]).split(" ", 1)[0]
        try:
            date.fromisoformat(bar_date)
        except ValueError as exc:
            raise RuntimeError(
                f"Futu 日K日期无效 {symbol}: {bar_date}"
            ) from exc
        values = pd.to_numeric(frame["close"], errors="coerce").dropna().tolist()
        if len(values) < window:
            raise RuntimeError(
                f"{symbol} K线不足: 需要 {window} 根，实际 {len(values)} 根"
            )
        values = [float(value) for value in values[-window:]]
        values[-1] = snapshot["price"]
        closes[symbol] = values
        snapshot["bar_date"] = bar_date
    return closes, snapshot_by_symbol


def close_futu_context(context: Any) -> None:
    if context is None:
        return
    try:
        context.close()
    except Exception as exc:
        eprint(f"警告: 关闭 Futu context 失败: {exc}")


def run_live(args: argparse.Namespace) -> int:
    try:
        from futu import AuType, OpenQuoteContext, RET_OK, SubType
    except ImportError as exc:
        raise RuntimeError("live 模式需要 futu-api") from exc

    preset = LIVE_PRESETS[args.mode]
    pairs = list(preset["pairs"])
    cash_symbols = set(preset["cash_symbols"])
    market_timezone = preset["timezone"]
    notification_time = preset["notification_time"]
    symbols = [symbol for symbol, _window in pairs]

    runtime = LiveRuntimePaths.from_argument(args.runtime_dir, args.mode)
    runtime.prepare()
    state = LiveState(runtime.state_file, pairs)
    host, port = resolve_live_connection(args.config, symbols)
    notifier = build_live_notifier(args.config)
    started = time.monotonic()
    context = None
    subscribed = False
    consecutive_errors = 0
    last_idle_log = -1e12
    running_script_hash = sha256_file(Path(__file__).resolve())

    try:
        with stop_event() as stopped:
            with RuntimeFileLock(runtime.lock_file):
                while not stopped.is_set():
                    now_monotonic = time.monotonic()
                    if args.duration and now_monotonic - started >= args.duration:
                        break
                    if sha256_file(Path(__file__).resolve()) != running_script_hash:
                        raise RuntimeError(
                            "脚本文件在 live 运行期间发生变化；退出并由PM2重启"
                        )

                    now = datetime.now(market_timezone)
                    today = now.date().isoformat()
                    if not args.once and (
                        not daily_evaluation_ready(
                            now, notification_time, market_timezone
                        )
                        or state.last_evaluation_date == today
                    ):
                        stopped.wait(min(args.interval, 60.0))
                        continue

                    try:
                        if context is None:
                            context = OpenQuoteContext(host=host, port=port)
                            subscribed = False
                        if not args.once and not is_live_trading_day(
                            context, symbols[0], today, RET_OK
                        ):
                            event = {
                                "type": "IDLE",
                                "strategy": LIVE_STRATEGY,
                                "mode": args.mode,
                                "message": "今日为非交易日，跳过信号计算",
                                "evaluation_date": today,
                                "emitted_at": now.isoformat(timespec="seconds"),
                            }
                            state.last_evaluation_date = today
                            state.record_snapshot(pairs, event)
                            print(json.dumps(event, ensure_ascii=False), flush=True)
                            consecutive_errors = 0
                            continue
                        if not subscribed:
                            subscribe_live_klines(
                                context,
                                symbols,
                                RET_OK,
                                SubType,
                            )
                            subscribed = True

                        closes, snapshots = fetch_live_market_data(
                            context,
                            pairs,
                            RET_OK,
                            SubType,
                            AuType,
                        )
                        bar_dates = {
                            snapshot["bar_date"] for snapshot in snapshots.values()
                        }
                        if len(bar_dates) != 1:
                            raise RuntimeError(
                                "ETF 最新日K日期不一致: "
                                + ", ".join(
                                    f"{symbol}={snapshot['bar_date']}"
                                    for symbol, snapshot in snapshots.items()
                                )
                            )
                        trading_date = next(iter(bar_dates))
                        if not args.once:
                            if trading_date != today:
                                if now_monotonic - last_idle_log >= 300:
                                    idle = {
                                        "type": "IDLE",
                                        "message": "今日无已完成日K，跳过信号计算",
                                        "latest_bar_date": trading_date,
                                        "emitted_at": now.isoformat(
                                            timespec="seconds"
                                        ),
                                    }
                                    print(
                                        json.dumps(idle, ensure_ascii=False),
                                        flush=True,
                                    )
                                    last_idle_log = now_monotonic
                                consecutive_errors = 0
                                stopped.wait(300.0)
                                continue
                            stale_symbols = []
                            for symbol, snapshot in snapshots.items():
                                quote_time = parse_futu_quote_time(
                                    snapshot["quote_time"], market_timezone
                                )
                                quote_age = (
                                    (now - quote_time).total_seconds()
                                    if quote_time is not None
                                    else math.inf
                                )
                                if (
                                    quote_time is None
                                    or quote_age < -60
                                    or quote_age > args.max_quote_age
                                ):
                                    stale_symbols.append(symbol)
                            if stale_symbols:
                                if now_monotonic - last_idle_log >= 300:
                                    idle = {
                                        "type": "IDLE",
                                        "message": "Futu 行情未更新，跳过信号计算",
                                        "symbols": stale_symbols,
                                        "emitted_at": now.isoformat(
                                            timespec="seconds"
                                        ),
                                    }
                                    print(
                                        json.dumps(idle, ensure_ascii=False),
                                        flush=True,
                                    )
                                    last_idle_log = now_monotonic
                                consecutive_errors = 0
                                stopped.wait(args.interval)
                                continue

                        scores = score_live_pairs(pairs, closes)
                        selected_symbol = select_live_symbol(
                            scores, args.min_score
                        )
                        previous_symbol = state.selected_symbol
                        action = live_transition_action(
                            previous_symbol,
                            selected_symbol,
                            cash_symbols,
                        )
                        target_symbol = (
                            selected_symbol
                            if selected_symbol not in cash_symbols
                            else None
                        )
                        windows = dict(pairs)
                        ranking = [
                            {
                                "symbol": symbol,
                                "window": windows[symbol],
                                "score": score,
                                "price": snapshots[symbol]["price"],
                                "quote_time": snapshots[symbol]["quote_time"],
                                "bar_date": snapshots[symbol]["bar_date"],
                            }
                            for symbol, score in sorted(
                                scores.items(),
                                key=lambda item: item[1],
                                reverse=True,
                            )
                        ]
                        event = {
                            "type": "SIGNAL",
                            "strategy": LIVE_STRATEGY,
                            "mode": args.mode,
                            "evaluation_date": trading_date,
                            "notification_time": notification_time.strftime("%H:%M"),
                            "timezone": str(market_timezone),
                            "action": action,
                            "previous_symbol": previous_symbol,
                            "selected_symbol": selected_symbol,
                            "target_symbol": target_symbol,
                            "cash_signal": (
                                selected_symbol is None
                                or selected_symbol in cash_symbols
                            ),
                            "min_score": (
                                args.min_score
                                if math.isfinite(args.min_score)
                                else None
                            ),
                            "ranking": ranking,
                            "emitted_at": now.isoformat(timespec="seconds"),
                        }
                        state.selected_symbol = selected_symbol
                        state.last_evaluation_date = trading_date
                        state.record_snapshot(pairs, event)
                        print(json.dumps(event, ensure_ascii=False), flush=True)
                        if notifier is not None:
                            notifier.notify(event)
                        consecutive_errors = 0
                    except Exception as exc:
                        close_futu_context(context)
                        context = None
                        subscribed = False
                        consecutive_errors += 1
                        event = {
                            "type": "ERROR",
                            "strategy": LIVE_STRATEGY,
                            "message": str(exc),
                            "consecutive_errors": consecutive_errors,
                            "emitted_at": now.isoformat(timespec="seconds"),
                        }
                        state.record_error(pairs, event)
                        eprint(json.dumps(event, ensure_ascii=False))
                        if notifier is not None:
                            notifier.notify(event)
                        if args.once or consecutive_errors >= args.max_errors:
                            raise

                    if args.once:
                        break
                    delay = args.interval
                    if consecutive_errors:
                        delay = min(
                            args.interval * (2 ** (consecutive_errors - 1)),
                            300.0,
                        )
                    stopped.wait(delay)
    finally:
        close_futu_context(context)
        if notifier is not None:
            notifier.close()
    return 0


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须大于0")
    return parsed


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return parsed


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Momentum rotation native backtest and Futu live monitor"
    )
    parser.add_argument(
        "mode",
        nargs="?",
        choices=["backtest", "optimize", "optimize-universe", *LIVE_PRESETS],
        default=DEFAULT_MODE,
        help="live-us: 美东16:10；live-cn: 北京15:10",
    )
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=DEFAULT_END)
    parser.add_argument("--config", default=None)
    parser.add_argument("--min-score", type=float, default=float("-inf"))
    parser.add_argument("--runtime-dir")
    parser.add_argument("--interval", type=positive_float, default=60.0)
    parser.add_argument(
        "--max-quote-age",
        type=positive_float,
        default=LIVE_MAX_QUOTE_AGE_SECONDS,
    )
    parser.add_argument(
        "--max-errors",
        type=positive_int,
        default=LIVE_MAX_CONSECUTIVE_ERRORS,
    )
    parser.add_argument("--duration", type=float, default=0)
    parser.add_argument("--once", action="store_true")
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    if args.duration < 0:
        raise ValueError("--duration 不能为负")
    if math.isnan(args.min_score):
        raise ValueError("--min-score 不能是 NaN")
    if args.mode in LIVE_PRESETS:
        if not args.runtime_dir:
            raise ValueError("live 模式必须指定 --runtime-dir")
        if not args.config:
            raise ValueError("live 模式必须指定 --config")
        return run_live(args)

    if args.mode == "optimize-universe":
        run_universe_optimization(args)
    elif args.mode == "optimize":
        run_momentum_optimization(make_backtest_config(args, DEFAULT_BACKTEST_SYMBOLS))
    else:
        run_momentum_backtest(make_backtest_config(args, DEFAULT_BACKTEST_SYMBOLS))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        eprint(f"错误: {exc}")
        raise SystemExit(2)
