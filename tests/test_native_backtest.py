import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
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


def _cost_aware_volume(price: float, params=None) -> int:
    """建仓份数：预留佣金与滑点后全仓买入。"""
    params = params or momentum.SimParams()
    return int(params.capital / (price * (1 + params.rate) + params.slippage))


def _gapless_rotation_histories(n_days: int = 60) -> dict[str, pd.DataFrame]:
    """A 先涨后跌、B 后段上涨，触发 A->B 轮动；open[t] = close[t-1]，无跳空。"""
    idx = pd.bdate_range("2026-01-01", periods=n_days)
    t = np.arange(n_days)
    closes = {
        "A": np.where(t < 25, 100 * 1.01 ** t, 100 * 1.01 ** 24 * 0.99 ** (t - 24)),
        "B": np.where(t < 25, 50.0, 50 * 1.012 ** (t - 24)),
        "US.SPY": np.full(n_days, 400.0),
    }
    frames = {}
    for name, close in closes.items():
        open_ = np.concatenate([[close[0]], close[:-1]])
        frames[name] = pd.DataFrame(
            {"Open": open_, "High": np.maximum(open_, close), "Low": np.minimum(open_, close),
             "Close": close, "Volume": 1e6},
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
        frame, trades = momentum._simulate_config(config, momentum.HistorySet(histories, histories))
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
        self.assertEqual(first["volume"], _cost_aware_volume(histories["A"].iloc[14]["Close"]))

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
        self.assertEqual(first["volume"], _cost_aware_volume(histories["A"].iloc[15]["Close"]))

    def test_costs_are_paid_from_cash_and_compound(self):
        """佣金与滑点必须从现金扣除：每次成交后现金不为负，净值与重放一致。"""
        histories = _gapless_rotation_histories()
        params = momentum.SimParams(window=5, rate=0.01, slippage=0.05)
        frame, trades, _ = momentum.simulate(
            histories, ["A", "B"], params, benchmark_symbol="US.SPY", rebalance=False,
        )
        self.assertGreaterEqual(len({t["vt_symbol"] for t in trades}), 2)
        cash, positions = float(params.capital), {"A": 0, "B": 0}
        for trade in trades:
            sign = 1 if trade["direction"] == "long" else -1
            gross = trade["volume"] * trade["price"]
            cash -= sign * gross + gross * params.rate + trade["volume"] * params.slippage
            positions[trade["vt_symbol"]] += sign * trade["volume"]
            self.assertGreaterEqual(cash, -1e-6, f"成本未预留，现金透支: {trade}")
        last = frame.index[-1]
        value = cash + sum(v * histories[s].loc[last, "Close"] for s, v in positions.items())
        self.assertAlmostEqual(frame["balance"].iloc[-1], value, places=4)


class AdjustedHistoryTest(unittest.TestCase):
    """回测口径：动量打分用富途前复权（与 live 一致），成交与净值用等比复权（分红再投资）。"""

    def test_prepare_history_converts_cached_forward_adjusted_bars(self):
        idx = pd.bdate_range("2026-01-01", periods=60)
        raw = 3.0 + 0.01 * np.arange(60)                      # 原始价
        ex_date, dividend = str(idx[20].date()), 0.1
        future = ("2026-04-15", 1.0, -0.1)                     # 拉取日之后的除权：缓存行情里尚未体现
        qfq = np.where(idx < pd.Timestamp(ex_date), raw - dividend, raw)
        fetched = datetime(2026, 3, 31, 16, 0, tzinfo=momentum.CN_TZ)
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(momentum, "HISTORY_CACHE_DIR", Path(tmp)), \
                patch.object(momentum, "OUTPUT_ROOT", Path(tmp) / "out"):
            for symbol, close, events in (("SH.510880", qfq, [[ex_date, 1.0, -dividend], list(future)]),
                                          ("SH.518880", raw, []), ("US.SPY", raw * 100, [])):
                frame = pd.DataFrame({"time_key": idx, "open": close, "high": close, "low": close,
                                      "close": close, "volume": 1e6})
                frame.to_csv(Path(tmp) / f"data_{symbol.replace('.', '_')}_K_DAY_{int(fetched.timestamp())}.csv",
                             index=False)
                (Path(tmp) / f"rehab_{symbol.replace('.', '_')}.json").write_text(json.dumps(
                    {"symbol": symbol, "fetched_on": "2026-04-20", "events": events}))
            config = momentum.BacktestConfig(symbols=["SH.510880", "SH.518880"],
                                             start="2026-01-01", end="2026-03-31", window=10)
            histories = momentum.prepare_history(config)
        np.testing.assert_allclose(histories.rank["SH.510880"]["Close"], qfq)
        # 等比复权 = 原始价 × (1 − 分红 / 除权前一日原始收盘)，只对除权日之前生效
        expected = raw * np.where(idx < pd.Timestamp(ex_date), 1 - dividend / raw[19], 1.0)
        np.testing.assert_allclose(histories.fill["SH.510880"]["Close"], expected)
        np.testing.assert_allclose(histories.fill["SH.518880"]["Close"], raw)

    def test_backtest_scores_on_rank_and_trades_on_fill(self):
        idx = pd.bdate_range("2026-01-01", periods=40)
        up, flat = 100 * 1.01 ** np.arange(40), np.full(40, 50.0)

        def frames(a, b):
            return {name: pd.DataFrame({"Open": c, "High": c, "Low": c, "Close": c, "Volume": 1e6}, index=idx)
                    for name, c in (("A", a), ("B", b), ("US.SPY", np.full(40, 400.0)))}
        rank = frames(up, flat)                                  # 前复权：A 动量第一
        fill = frames(np.full(40, 80.0), 50 * 1.01 ** np.arange(40))   # 若按等比复权打分会选 B
        config = momentum.BacktestConfig(symbols=["A", "B"], start="2026-01-01", end="2026-03-01", window=10)
        frame, trades = momentum._simulate_config(config, momentum.HistorySet(rank, fill))
        self.assertEqual([t["vt_symbol"] for t in trades], ["A"])
        trade = trades[0]
        self.assertEqual(trade["price"], fill["A"].loc[trade["datetime"], "Open"])
        params = momentum.SimParams()
        cash = params.capital - trade["volume"] * (trade["price"] * (1 + params.rate) + params.slippage)
        self.assertAlmostEqual(frame["balance"].iloc[-1], cash + trade["volume"] * 80.0, places=4)


if __name__ == "__main__":
    unittest.main()
