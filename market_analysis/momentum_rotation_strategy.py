from __future__ import annotations

import ast
import math
import argparse
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path
from time import perf_counter
from typing import Any

import pandas as pd


def bootstrap_vnpy_workspace_for_script() -> None:
    project_root = Path(__file__).resolve().parents[1]
    data_root = project_root / "data"
    data_root.mkdir(parents=True, exist_ok=True)
    data_root.joinpath(".vntrader").mkdir(parents=True, exist_ok=True)

    for path in (project_root, Path(__file__).resolve().parent):
        path_str = str(path)
        if path_str not in sys.path:
            sys.path.insert(0, path_str)

    os.chdir(data_root)


if __name__ == "__main__":
    bootstrap_vnpy_workspace_for_script()

import numpy as np

from vnpy.trader.constant import Direction
from vnpy.trader.object import BarData, TradeData
from vnpy.trader.utility import ArrayManager
from vnpy_portfoliostrategy import StrategyTemplate


class MomentumRotationStrategy(StrategyTemplate):
    """Daily multi-asset momentum rotation strategy for vn.py portfolio backtesting."""

    author = "futu_trends"

    regression_window: int = 22
    initial_capital: int = 1_000_000
    allocation: float = 1.0
    min_score: float = float("-inf")
    cash_symbols: tuple[str, ...] = ()

    holding_symbol: str = ""
    sizing_cash: float = 0
    last_target_volume: int = 0

    parameters = [
        "regression_window",
        "initial_capital",
        "allocation",
        "min_score",
        "cash_symbols",
    ]

    variables = [
        "holding_symbol",
        "sizing_cash",
        "last_target_volume",
    ]

    def __init__(self, strategy_engine, strategy_name: str, vt_symbols: list[str], setting: dict) -> None:
        super().__init__(strategy_engine, strategy_name, vt_symbols, setting)
        self.ams: dict[str, ArrayManager] = {}
        self.last_prices: dict[str, float] = {}
        self.last_scores: dict[str, float] = {}
        self.cash_vt_symbols = self.normalize_cash_symbols(self.cash_symbols)
        self.sizing_cash = float(self.initial_capital)

    def on_init(self) -> None:
        size = max(self.regression_window + 5, 10)
        self.ams = {vt_symbol: ArrayManager(size) for vt_symbol in self.vt_symbols}
        self.write_log("Momentum rotation strategy initialized")

    def on_start(self) -> None:
        self.write_log("Momentum rotation strategy started")

    def on_stop(self) -> None:
        self.write_log("Momentum rotation strategy stopped")

    def on_bars(self, bars: dict[str, BarData]) -> None:
        for vt_symbol, bar in bars.items():
            self.last_prices[vt_symbol] = bar.close_price
            self.ams[vt_symbol].update_bar(bar)

        scores = self.calculate_scores()
        if not scores:
            self.put_event()
            return

        self.last_scores = scores
        target_symbol = self.select_target_symbol(scores)
        targets = self.calculate_targets(target_symbol)

        changed = False
        for vt_symbol in self.vt_symbols:
            target = targets[vt_symbol]
            if self.get_target(vt_symbol) != target:
                self.set_target(vt_symbol, target)
                changed = True

        if changed:
            self.holding_symbol = target_symbol or ""
            self.last_target_volume = targets.get(target_symbol, 0) if target_symbol else 0
            self.rebalance_portfolio(bars)

        self.put_event()

    def update_trade(self, trade: TradeData) -> None:
        super().update_trade(trade)

        size = self.get_size(trade.vt_symbol)
        turnover = trade.price * trade.volume * size
        if trade.direction == Direction.LONG:
            self.sizing_cash -= turnover
        else:
            self.sizing_cash += turnover

    def calculate_scores(self) -> dict[str, float]:
        scores: dict[str, float] = {}
        for vt_symbol in self.vt_symbols:
            am = self.ams[vt_symbol]
            if not am.inited:
                return {}

            closes = am.close[-self.regression_window:]
            score = self.calculate_momentum_score(closes)
            if math.isfinite(score):
                scores[vt_symbol] = score

        return scores

    def select_target_symbol(self, scores: dict[str, float]) -> str | None:
        target_symbol = max(scores, key=scores.get)
        if scores[target_symbol] < self.min_score:
            return None
        return target_symbol

    def calculate_targets(self, target_symbol: str | None) -> dict[str, int]:
        targets: defaultdict[str, int] = defaultdict(int)
        if not target_symbol:
            return targets

        if target_symbol in self.cash_vt_symbols:
            return targets

        price = self.last_prices.get(target_symbol, 0)
        if price <= 0:
            return targets

        portfolio_value = self.calculate_portfolio_value()
        target_notional = max(portfolio_value, 0) * self.allocation
        size = self.get_size(target_symbol)
        volume = int(target_notional / (price * size))
        targets[target_symbol] = max(volume, 0)
        return targets

    @staticmethod
    def normalize_cash_symbols(symbols: list[str] | tuple[str, ...]) -> set[str]:
        cash_vt_symbols: set[str] = set()
        for symbol in symbols:
            normalized = symbol.strip().upper()
            if not normalized:
                continue
            if normalized.startswith("US."):
                cash_vt_symbols.add(f"{normalized.removeprefix('US.')}.SMART")
            else:
                cash_vt_symbols.add(normalized)
        return cash_vt_symbols

    def calculate_portfolio_value(self) -> float:
        value = self.sizing_cash
        for vt_symbol in self.vt_symbols:
            price = self.last_prices.get(vt_symbol)
            if price is None:
                continue
            value += self.get_pos(vt_symbol) * price * self.get_size(vt_symbol)
        return value

    @staticmethod
    def calculate_momentum_score(data: np.ndarray) -> float:
        if len(data) < 2 or np.any(data <= 0):
            return float("-inf")

        x = np.arange(len(data))
        y = np.log(data)
        slope, intercept = np.polyfit(x, y, 1)
        annualized_return = math.exp(slope * 250) - 1

        y_pred = slope * x + intercept
        ss_res = np.sum((y - y_pred) ** 2)
        ss_tot = np.sum((y - np.mean(y)) ** 2)
        r_squared = 1 - ss_res / ss_tot if ss_tot else 0
        return annualized_return * r_squared


# Ten-year universe optimization snapshot, 2016-06-12 to 2026-06-12, benchmark US.SPY.
# Candidate pool: US.QQQ, US.GLD, US.SPY, US.FXI, US.UUP, US.TLT.
# US.UUP participates in momentum ranking as a cash/timing signal. If selected, the strategy holds cash.
# Ranked by Sortino ratio after selecting 4 symbols and optimizing regression_window in 20..30:
# 1. US.QQQ US.SPY US.FXI US.UUP, window=21, total=521.47%, annual=51.88%, max_dd=-28.28%, Sharpe=0.9285, Sortino=3.9316, Calmar=1.8342, excess_vs_spy=205.78%
# 2. US.QQQ US.GLD US.SPY US.UUP, window=22, total=419.70%, annual=41.75%, max_dd=-25.40%, Sharpe=0.9051, Sortino=3.2264, Calmar=1.6440, excess_vs_spy=104.01%
# 3. US.QQQ US.GLD US.SPY US.FXI, window=22, total=520.53%, annual=51.78%, max_dd=-27.78%, Sharpe=0.7683, Sortino=3.1438, Calmar=1.8643, excess_vs_spy=204.84%
# Allocation note: US.QQQ/US.GLD/US.SPY/US.UUP is easier to interpret as equity/gold/equity-beta/cash
# timing, while US.QQQ/US.SPY/US.FXI/US.UUP adds non-US equity risk and fits a more aggressive timing model.

DEFAULT_MODE = "optimize-universe"
DEFAULT_START = "2016-06-12"
DEFAULT_END = "2026-06-12"
DEFAULT_CAPITAL = 1_000_000
DEFAULT_DATA_SOURCE = "project"
DEFAULT_MAX_WORKERS = 4
DEFAULT_OPTIMIZATION_METHOD = "bf"
DEFAULT_OPTIMIZATION_TARGET = "sortino_ratio"
DEFAULT_CANDIDATE_SYMBOLS = ["US.QQQ", "US.GLD", "US.SPY", "US.UUP", "US.FXI", "US.TLT"]
# DEFAULT_CANDIDATE_SYMBOLS = ["SZ.159941", "SH.518880", "SZ.159949", "SH.510300", "SH.510880"]
DEFAULT_BACKTEST_SYMBOLS = DEFAULT_CANDIDATE_SYMBOLS[:4]
DEFAULT_UNIVERSE_SIZE = 4
DEFAULT_BENCHMARK_SYMBOL = "US.SPY"
DEFAULT_CASH_SYMBOLS = ["US.UUP"]
DEFAULT_STRATEGY_SETTING = {
    "regression_window": 22,
    "allocation": 1.0,
    "min_score": float("-inf"),
    "cash_symbols": DEFAULT_CASH_SYMBOLS,
}
DEFAULT_OPTIMIZATION_PARAMETERS = [
    ("regression_window", 20, 30, 1),
]


def make_optimization_parameters(parameter_cls: type) -> list[Any]:
    return [
        parameter_cls(name, start, end, step)
        for name, start, end, step in DEFAULT_OPTIMIZATION_PARAMETERS
    ]


def make_backtest_config(
    args: argparse.Namespace,
    symbols: list[str],
    config_cls: type,
    data_source: str | None = None,
) -> Any:
    strategy_setting = dict(DEFAULT_STRATEGY_SETTING)
    strategy_setting["initial_capital"] = DEFAULT_CAPITAL

    return config_cls(
        symbols=symbols,
        start=args.start,
        end=args.end,
        capital=DEFAULT_CAPITAL,
        data_source=data_source or DEFAULT_DATA_SOURCE,
        config_path=args.config,
        benchmark_symbol=DEFAULT_BENCHMARK_SYMBOL,
        strategy_setting=strategy_setting,
    )


def export_universe_results(rows: list[dict[str, Any]], output_root: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = output_root / f"momentumrotationstrategy_universe_optimization_{stamp}.csv"
    pd.DataFrame(rows).sort_values("target", ascending=False).to_csv(path, index=False)
    return path


def write_universe_progress(rows: list[dict[str, Any]], path: Path) -> None:
    if rows:
        pd.DataFrame(rows).sort_values("target", ascending=False).to_csv(path, index=False)


def format_elapsed(seconds: float) -> str:
    total_seconds = max(int(seconds), 0)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{seconds:02d}s"
    if minutes:
        return f"{minutes}m{seconds:02d}s"
    return f"{seconds}s"


def parse_setting_text(setting_text: str) -> dict[str, Any]:
    try:
        safe_text = re.sub(r"(?<![\w.])-inf(?![\w.])", "-1e309", setting_text)
        safe_text = re.sub(r"(?<![\w.])inf(?![\w.])", "1e309", safe_text)
        setting = ast.literal_eval(safe_text)
    except (SyntaxError, ValueError):
        return {}

    return setting if isinstance(setting, dict) else {}


def run_universe_optimization(
    args: argparse.Namespace,
    config_cls: type,
    parameter_cls: type,
    output_root: Path,
    prepare_history_func: Any,
    run_optimization_func: Any,
) -> list[dict[str, Any]]:
    candidates = DEFAULT_CANDIDATE_SYMBOLS
    optimization_parameters = make_optimization_parameters(parameter_cls)
    rows: list[dict[str, Any]] = []
    combos = list(combinations(candidates, DEFAULT_UNIVERSE_SIZE))
    combo_source = DEFAULT_DATA_SOURCE
    started_at = perf_counter()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    output_root.mkdir(parents=True, exist_ok=True)
    progress_path = output_root / f"momentumrotationstrategy_universe_progress_{stamp}.csv"

    print(
        "Universe optimization started: "
        f"candidates={len(candidates)}, choose={DEFAULT_UNIVERSE_SIZE}, "
        f"combos={len(combos)}, target={DEFAULT_OPTIMIZATION_TARGET}, "
        f"progress={progress_path}",
        flush=True,
    )

    if DEFAULT_DATA_SOURCE == "project":
        print(f"Preparing history data for candidate universe: {' '.join(candidates)}", flush=True)
        prepare_history_func(make_backtest_config(args, candidates, config_cls))
        combo_source = "database"

    for index, combo in enumerate(combos, start=1):
        symbols = list(combo)
        combo_started_at = perf_counter()
        print(f"Universe {index}/{len(combos)} started: {' '.join(symbols)}", flush=True)
        config = make_backtest_config(args, symbols, config_cls, data_source=combo_source)
        results = run_optimization_func(
            MomentumRotationStrategy,
            config,
            optimization_parameters,
            target=DEFAULT_OPTIMIZATION_TARGET,
            method=DEFAULT_OPTIMIZATION_METHOD,
            max_workers=DEFAULT_MAX_WORKERS,
        )
        best_setting, best_target, best_statistics = results[0]
        row = {
            "symbols": " ".join(symbols),
            "target": best_target,
            "setting": best_setting,
        }
        for key, value in parse_setting_text(best_setting).items():
            row[f"param_{key}"] = value
        row.update(best_statistics)
        rows.append(row)
        write_universe_progress(rows, progress_path)
        print(
            f"Universe {index}/{len(combos)} done: "
            f"target={best_target:.6f}, elapsed={format_elapsed(perf_counter() - combo_started_at)}, "
            f"total_elapsed={format_elapsed(perf_counter() - started_at)}",
            flush=True,
        )

    path = export_universe_results(rows, output_root)
    print(
        f"Universe optimization result: {path} "
        f"(elapsed={format_elapsed(perf_counter() - started_at)}, progress={progress_path})",
        flush=True,
    )
    try:
        from backtest import print_result_rows
    except ImportError:
        print_result_rows = None
    if print_result_rows:
        print_result_rows(
            sorted(rows, reverse=True, key=lambda row: row["target"]),
            "Top 3 universe optimization results:",
            limit=3,
        )
    return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Momentum rotation vn.py backtest")
    parser.add_argument(
        "mode",
        nargs="?",
        choices=["backtest", "optimize", "optimize-universe"],
        default=DEFAULT_MODE,
    )
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=DEFAULT_END)
    parser.add_argument("--config", default=None)
    return parser.parse_args()


def main() -> None:
    from backtest import (
        BacktestConfig,
        OUTPUT_ROOT,
        OptimizationParameter,
        prepare_history_data,
        run_strategy_backtest,
        run_strategy_optimization,
    )

    args = parse_args()
    if args.mode == "optimize-universe":
        run_universe_optimization(
            args,
            BacktestConfig,
            OptimizationParameter,
            OUTPUT_ROOT,
            prepare_history_data,
            run_strategy_optimization,
        )
    elif args.mode == "optimize":
        config = make_backtest_config(args, DEFAULT_BACKTEST_SYMBOLS, BacktestConfig)
        optimization_parameters = make_optimization_parameters(OptimizationParameter)
        run_strategy_optimization(
            MomentumRotationStrategy,
            config,
            optimization_parameters,
            target=DEFAULT_OPTIMIZATION_TARGET,
            method=DEFAULT_OPTIMIZATION_METHOD,
            max_workers=DEFAULT_MAX_WORKERS,
        )
    else:
        config = make_backtest_config(args, DEFAULT_BACKTEST_SYMBOLS, BacktestConfig)
        run_strategy_backtest(MomentumRotationStrategy, config)


if __name__ == "__main__":
    main()
