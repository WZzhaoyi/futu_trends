from __future__ import annotations

import math
import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path


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

    holding_symbol: str = ""
    sizing_cash: float = 0
    last_target_volume: int = 0

    parameters = [
        "regression_window",
        "initial_capital",
        "allocation",
        "min_score",
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

        price = self.last_prices.get(target_symbol, 0)
        if price <= 0:
            return targets

        portfolio_value = self.calculate_portfolio_value()
        target_notional = max(portfolio_value, 0) * self.allocation
        size = self.get_size(target_symbol)
        volume = int(target_notional / (price * size))
        targets[target_symbol] = max(volume, 0)
        return targets

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


DEFAULT_BACKTEST_SYMBOLS = ["US.QQQ", "US.GLD", "US.SPY", "US.FXI"]
DEFAULT_STRATEGY_SETTING = {
    "regression_window": 22,
    "allocation": 1.0,
    "min_score": float("-inf"),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Momentum rotation vn.py backtest")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_BACKTEST_SYMBOLS)
    parser.add_argument("--start", default="2014-09-09")
    parser.add_argument("--end", default="2025-09-09")
    parser.add_argument("--capital", type=int, default=1_000_000)
    parser.add_argument("--source", choices=["project", "database"], default="project")
    parser.add_argument("--config", default=None)
    parser.add_argument("--refresh-data", action="store_true")
    parser.add_argument("--proxy", default=None)
    parser.add_argument("--no-proxy", action="store_true")
    parser.add_argument("--show-chart", action="store_true")
    return parser.parse_args()


def main() -> None:
    from backtest import BacktestConfig, run_strategy_backtest

    args = parse_args()
    strategy_setting = dict(DEFAULT_STRATEGY_SETTING)
    strategy_setting["initial_capital"] = args.capital

    config = BacktestConfig(
        symbols=args.symbols,
        start=args.start,
        end=args.end,
        capital=args.capital,
        data_source=args.source,
        config_path=args.config,
        refresh_data=args.refresh_data,
        proxy=args.proxy,
        no_proxy=args.no_proxy,
        show_chart=args.show_chart,
        strategy_setting=strategy_setting,
    )
    run_strategy_backtest(MomentumRotationStrategy, config)


if __name__ == "__main__":
    main()
