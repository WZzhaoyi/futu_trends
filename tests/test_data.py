"""data.py 的交易状态缓存：按交易所前缀分键（沪/深/港/美各自独立）。"""

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "data.py"
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("data_tested", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
data = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = data
SPEC.loader.exec_module(data)


GLOBAL_STATE = {
    "market_sh": "MORNING",          # 盘中
    "market_sz": "CLOSED",           # 非盘中
    "market_hk": "AFTER_HOURS_END",  # 非盘中
    "market_us": "NIGHT",            # 盘中
}


class FakeQuoteContext:
    instances = []

    def __init__(self, host, port):
        self.__class__.instances.append(self)
        self.closed = False

    def get_global_state(self):
        return 0, dict(GLOBAL_STATE)

    def close(self):
        self.closed = True


class TradingStateKeyTest(unittest.TestCase):
    def setUp(self):
        data._state_cache = None
        data._state_ts = 0
        FakeQuoteContext.instances.clear()

    def query(self, code):
        with (
            patch.object(data, "opend_alive", return_value=True),
            patch.object(data.ft, "OpenQuoteContext", FakeQuoteContext),
        ):
            return data._is_trading(code, "127.0.0.1", 11111)

    def test_each_exchange_prefix_reads_its_own_state_field(self):
        self.assertTrue(self.query("SH.510300"))
        self.assertFalse(self.query("SZ.159941"))
        self.assertFalse(self.query("HK.00700"))
        self.assertTrue(self.query("US.QQQ"))
        # 四次查询共用一次 get_global_state（TTL 内复用）
        self.assertEqual(len(FakeQuoteContext.instances), 1)

    def test_unknown_prefix_is_treated_as_intraday_without_querying(self):
        with (
            patch.object(data, "opend_alive", return_value=True),
            patch.object(
                data.ft,
                "OpenQuoteContext",
                side_effect=AssertionError("未知市场不应查询 futu"),
            ),
        ):
            self.assertTrue(data._is_trading("XX.123", "127.0.0.1", 11111))


if __name__ == "__main__":
    unittest.main()
