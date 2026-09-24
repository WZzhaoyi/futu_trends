import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path

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
    def test_grid_and_formal_backtest_do_not_rebalance_unchanged_symbol(self):
        histories = _fake_histories()
        params = momentum.SimParams(window=10)
        grid = momentum._grid_worker({
            "histories": histories,
            "symbols": ["A", "B"],
            "params": params,
            "benchmark": "B",
            "uname": "A B",
        })
        config = momentum.BacktestConfig(
            symbols=["A", "B"], start="2026-01-01", end="2026-03-01", window=10,
        )
        frame, trades = momentum._simulate_config(config, histories)
        # 跳空建仓后目标股数会变化；同标的持仓仍只应成交一次。
        self.assertEqual(grid["rotDays"], 1)
        self.assertEqual(frame["trade_count"].sum(), 1)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["vt_symbol"], "A")
        self.assertEqual(trades[0]["direction"], "long")

        artifact_dir = os.environ.get("FUTU_TEST_ARTIFACT_DIR")
        if artifact_dir:
            output = Path(artifact_dir)
            output.mkdir(parents=True, exist_ok=True)
            frame.to_csv(output / "momentum-daily.csv")
            (output / "momentum-trades.json").write_text(
                json.dumps({"grid": grid, "trades": trades}, default=str, indent=2) + "\n",
                encoding="utf-8",
            )

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
        # window=10 + 5 根预热：第15条收盘决策，第16条开盘成交。
        expected_date = histories["A"].index[15]
        self.assertEqual(pd.Timestamp(first["datetime"]), expected_date)
        self.assertEqual(first["price"], histories["A"].loc[expected_date, "Open"])
        self.assertNotEqual(first["price"], histories["A"].loc[expected_date, "Close"])
        self.assertEqual(first["volume"], int(1_000_000 / histories["A"].iloc[14]["Close"]))

    def test_market_open_suspension_carries_over(self):
        histories = _fake_histories()
        # 首个决策日的次日（建仓执行日）A 停牌 open=0 → 顺延至再下一日成交
        dates = histories["A"].index
        suspended = dates[15]
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
        expected_date = dates[16]
        self.assertEqual(pd.Timestamp(first["datetime"]), expected_date)
        self.assertEqual(first["price"], histories["A"].loc[expected_date, "Open"])
        # 未建仓时仍按停牌日收盘重新计算目标数量，恢复后执行该目标。
        self.assertEqual(first["volume"], int(1_000_000 / histories["A"].iloc[15]["Close"]))

if __name__ == "__main__":
    unittest.main()
