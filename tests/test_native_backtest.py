import importlib.util
import sys
import unittest
from pathlib import Path

import pandas as pd


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "market_analysis"
    / "momentum_rotation_strategy.py"
)
SPEC = importlib.util.spec_from_file_location("momentum_backtest_tested", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
momentum = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = momentum
SPEC.loader.exec_module(momentum)


class MomentumFillTest(unittest.TestCase):
    def test_backtest_uses_futu_source_from_config(self):
        config = momentum._futu_config(
            str(momentum.PROJECT_ROOT / "config_template.ini"),
            "US.QQQ",
        )

        self.assertEqual(config.get("CONFIG", "DATA_SOURCE"), "futu")

    def test_limit_order_uses_better_next_open(self):
        bar = pd.Series({"Open": 9, "High": 11, "Low": 8, "Close": 10})

        self.assertEqual(momentum._fill_price("long", 10, bar), 9)
        self.assertEqual(momentum._fill_price("short", 8, bar), 9)

    def test_unfilled_limit_order_remains_pending(self):
        bar = pd.Series({"Open": 12, "High": 13, "Low": 11, "Close": 12})

        self.assertIsNone(momentum._fill_price("long", 10, bar))


if __name__ == "__main__":
    unittest.main()
