import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "market_analysis"
    / "momentum_rotation_strategy.py"
)
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("momentum_backtest_tested", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
momentum = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = momentum
SPEC.loader.exec_module(momentum)


def _fake_histories(n_days: int = 40) -> dict[str, pd.DataFrame]:
    """合成两标的 OHLCV：A 单边上涨（动量第一），B 横盘（动量≈0）。

    40 交易日，足够 window=10 预热；A 的 open 故意在个别日偏离 close，
    便于验证"次日开盘价成交"。
    """
    idx = pd.bdate_range("2026-01-01", periods=n_days)
    frames = {}
    for name, base in (("A", 100.0), ("B", 50.0), ("US.SPY", 400.0)):
        close = base * np.linspace(1.0, 1.5, n_days) if name == "A" else np.full(n_days, base)
        open_ = close.copy()
        # 第 16 日开盘跳空 +2（验证买入按开盘价而非收盘价）
        if name == "A":
            open_[15] = close[15] + 2.0
        high = np.maximum(open_, close) * 1.01
        low = np.minimum(open_, close) * 0.99
        frames[name] = pd.DataFrame(
            {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": 1e6},
            index=idx,
        )
    return frames


class MomentumOpenFillTest(unittest.TestCase):
    def test_grid_worker_uses_symbol_changes_without_share_rebalancing(self):
        frame = pd.DataFrame({"trade_count": [0]})
        stats = {
            "total_return": 1.0,
            "max_ddpercent": -1.0,
            "sortino_ratio": 1.0,
            "calmar_ratio": 1.0,
            "sharpe_ratio": 1.0,
            "total_commission": 0.0,
        }
        params = momentum.SimParams(window=10, cooldown=3)
        with patch.object(
            momentum,
            "simulate",
            return_value=(frame, [], stats),
        ) as simulate:
            momentum._grid_worker(
                {
                    "histories": {},
                    "symbols": ["A", "B"],
                    "params": params,
                    "benchmark": "B",
                    "uname": "A B",
                }
            )

        self.assertFalse(simulate.call_args.kwargs["rebalance"])

    def test_formal_backtest_uses_the_same_no_rebalance_policy(self):
        histories = _fake_histories()
        config = momentum.BacktestConfig(
            symbols=["A", "B"],
            start="2026-01-01",
            end="2026-03-01",
            window=10,
        )
        result_frame = pd.DataFrame({"trade_count": [0]})
        with patch.object(
            momentum,
            "simulate",
            return_value=(result_frame, [], {}),
        ) as simulate:
            momentum._simulate_config(config, histories)

        self.assertFalse(simulate.call_args.kwargs["rebalance"])

    def test_market_open_fills_at_next_day_open(self):
        histories = _fake_histories()
        frame, trades, stats = momentum.simulate(
            histories,
            ["A", "B"],
            momentum.SimParams(window=10),
            benchmark_symbol="B",
        )
        # 首次建仓发生在首个决策日的次日，成交价 = 当日开盘价（跳空 +2 亦按开盘价）
        first = trades[0]
        self.assertEqual(first["direction"], "long")
        self.assertEqual(first["vt_symbol"], "A")
        bar_date = pd.Timestamp(first["datetime"]).normalize()
        open_on_bar = float(histories["A"].loc[bar_date, "Open"])
        self.assertEqual(first["price"], open_on_bar)

    def test_market_open_suspension_carries_over(self):
        histories = _fake_histories()
        # 首个决策日的次日（建仓执行日）A 停牌 open=0 → 顺延至再下一日成交
        dates = histories["A"].index
        decision_day = dates[momentum.SimParams(window=10).warmup_extra + 10 - 1]
        suspended = dates[list(dates).index(decision_day) + 1]
        histories["A"].loc[suspended, "Open"] = 0.0
        histories["A"].loc[suspended, "High"] = 0.0
        histories["A"].loc[suspended, "Low"] = 0.0

        frame, trades, stats = momentum.simulate(
            histories,
            ["A", "B"],
            momentum.SimParams(window=10),
            benchmark_symbol="B",
        )
        first = trades[0]
        self.assertNotEqual(pd.Timestamp(first["datetime"]).normalize(), suspended)
        bar_date = pd.Timestamp(first["datetime"]).normalize()
        self.assertEqual(
            first["price"], float(histories["A"].loc[bar_date, "Open"])
        )

    def test_no_limit_price_in_trades(self):
        histories = _fake_histories()
        frame, trades, stats = momentum.simulate(
            histories,
            ["A", "B"],
            momentum.SimParams(window=10),
            benchmark_symbol="B",
        )
        for trade in trades:
            self.assertIn("price", trade)
            self.assertGreater(trade["price"], 0)


if __name__ == "__main__":
    unittest.main()
