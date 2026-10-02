import configparser
import importlib.util
import sys
import tempfile
import types
import unittest
from datetime import date, datetime, time
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import tools


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "market_analysis"
    / "trading_calendar.py"
)
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location(
    "trading_calendar_tested",
    MODULE_PATH,
)
assert SPEC is not None and SPEC.loader is not None
calendar = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = calendar
SPEC.loader.exec_module(calendar)

SHANGHAI = ZoneInfo("Asia/Shanghai")
NEW_YORK = ZoneInfo("America/New_York")
HONG_KONG = ZoneInfo("Asia/Hong_Kong")


class FakeQuoteContext:
    instances = []
    ret = 0
    days = []
    days_by_symbol = None

    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.closed = False
        self.requests = []
        self.__class__.instances.append(self)

    def request_trading_days(self, start, end, code):
        self.requests.append((start, end, code))
        days = self.__class__.days
        if self.__class__.days_by_symbol is not None:
            days = self.__class__.days_by_symbol.get(code, [])
        return self.__class__.ret, days

    def close(self):
        self.closed = True


def fake_futu():
    module = types.ModuleType("futu")
    module.OpenQuoteContext = FakeQuoteContext
    module.RET_OK = 0
    return module


class TradingCalendarTestBase(unittest.TestCase):
    def setUp(self):
        FakeQuoteContext.instances.clear()
        FakeQuoteContext.ret = 0
        FakeQuoteContext.days = []
        FakeQuoteContext.days_by_symbol = None
        self._cache = tempfile.TemporaryDirectory()
        self.addCleanup(self._cache.cleanup)
        self.cache_dir = Path(self._cache.name)

    def query(self, market, trading_date=None, **kwargs):
        with patch.dict(sys.modules, {"futu": fake_futu()}):
            return calendar.trading_day_type(
                market,
                trading_date,
                cache_dir=self.cache_dir,
                **kwargs,
            )


class MarketSpecTest(TradingCalendarTestBase):
    def test_each_market_owns_its_timezone_and_calendar_anchor(self):
        self.assertEqual(calendar.MARKETS["US"].timezone, NEW_YORK)
        self.assertEqual(calendar.MARKETS["US"].calendar_symbol, "US.QQQ")
        self.assertEqual(calendar.MARKETS["CN"].timezone, SHANGHAI)
        self.assertEqual(calendar.MARKETS["CN"].calendar_symbol, "SH.000001")
        self.assertEqual(calendar.MARKETS["HK"].timezone, HONG_KONG)
        self.assertEqual(calendar.MARKETS["HK"].calendar_symbol, "HK.800000")

    def test_market_has_no_default_and_must_be_explicit(self):
        for missing in ("", None, "   "):
            with self.subTest(market=missing):
                with self.assertRaisesRegex(ValueError, "必须显式指定市场"):
                    calendar.today_in(missing)

    def test_unknown_market_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "未知市场"):
            calendar.today_in("JP")


class MarketClockTest(TradingCalendarTestBase):
    def test_shanghai_morning_maps_to_the_previous_us_date(self):
        morning = datetime(2026, 10, 1, 9, 53, tzinfo=SHANGHAI)

        self.assertEqual(calendar.today_in("US", morning), date(2026, 9, 30))
        self.assertEqual(calendar.today_in("CN", morning), date(2026, 10, 1))

    def test_offsets_follow_dst_instead_of_a_fixed_hour_count(self):
        summer = calendar.now_in("US", datetime(2026, 10, 1, 9, 53, tzinfo=SHANGHAI))
        winter = calendar.now_in("US", datetime(2026, 12, 1, 9, 53, tzinfo=SHANGHAI))

        self.assertEqual(summer.utcoffset().total_seconds(), -4 * 3600)
        self.assertEqual(winter.utcoffset().total_seconds(), -5 * 3600)
        self.assertEqual(winter.date(), date(2026, 11, 30))

    def test_local_conversion_round_trips(self):
        morning = datetime(2026, 10, 1, 9, 53, tzinfo=SHANGHAI)
        converted = calendar.now_in("US", morning)

        self.assertEqual(converted.astimezone(SHANGHAI), morning)
        self.assertEqual(calendar.to_local(converted).astimezone(SHANGHAI), morning)

    def test_latest_closed_date_follows_the_market_close_time(self):
        before_close = datetime(2026, 10, 1, 3, 0, tzinfo=SHANGHAI)   # 美东 09-30 15:00
        after_close = datetime(2026, 10, 1, 9, 53, tzinfo=SHANGHAI)   # 美东 09-30 21:53

        self.assertEqual(
            calendar.latest_closed_date("US", time(16, 10), before_close),
            date(2026, 9, 29),
        )
        self.assertEqual(
            calendar.latest_closed_date("US", time(16, 10), after_close),
            date(2026, 9, 30),
        )


def make_config(market="CN", **extra):
    config = configparser.ConfigParser()
    config["CONFIG"] = {
        "FUTU_HOST": "127.0.0.1",
        "FUTU_PORT": "11111",
        **extra,
    }
    if market is not None:
        config.set("CONFIG", "MARKET", market)
    return config


class ConfigMarketTest(unittest.TestCase):
    """MARKET / TRADING_DAY_GATE 由日历模块统一解析，各策略不再各读各的"""

    def test_market_is_normalised_and_may_list_several(self):
        self.assertEqual(calendar.markets_from_config(make_config("cn")), ("CN",))
        self.assertEqual(
            calendar.markets_from_config(make_config(" cn , hk ")),
            ("CN", "HK"),
        )

    def test_missing_market_is_rejected_instead_of_defaulting(self):
        for missing in (None, "", "   "):
            with self.subTest(market=missing):
                with self.assertRaisesRegex(ValueError, "缺少 CONFIG/MARKET"):
                    calendar.markets_from_config(make_config(missing))

    def test_unknown_market_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "未知市场"):
            calendar.markets_from_config(make_config("CN,JP"))

    def test_gate_is_required_and_not_defaulted(self):
        with self.assertRaisesRegex(ValueError, "缺少 CONFIG/TRADING_DAY_GATE"):
            calendar.trading_day_gate_enabled(make_config("CN"))
        with self.assertRaisesRegex(ValueError, "缺少 CONFIG/TRADING_DAY_GATE"):
            calendar.trading_day_gate_enabled(make_config("CN", TRADING_DAY_GATE=""))

    def test_gate_accepts_the_documented_spellings(self):
        for raw in ("on", "ON", "true", "1", "yes"):
            with self.subTest(value=raw):
                self.assertTrue(
                    calendar.trading_day_gate_enabled(
                        make_config("CN", TRADING_DAY_GATE=raw)
                    )
                )
        for raw in ("off", "OFF", "false", "0", " no "):
            with self.subTest(value=raw):
                self.assertFalse(
                    calendar.trading_day_gate_enabled(
                        make_config("CN", TRADING_DAY_GATE=raw)
                    )
                )

    def test_invalid_gate_value_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "TRADING_DAY_GATE 取值无效"):
            calendar.trading_day_gate_enabled(
                make_config("CN", TRADING_DAY_GATE="maybe")
            )


class CodeMarketConsistencyTest(unittest.TestCase):
    def test_code_prefix_markets_are_supported_by_the_calendar(self):
        """tools 的「代码前缀→市场」映射不能指向日历模块不认识的市场"""
        self.assertTrue(set(tools.CODE_MARKETS.values()) <= set(calendar.MARKETS))


class TradingDayQueryTest(TradingCalendarTestBase):
    def test_holiday_is_reported_as_a_non_trading_day(self):
        # A股国庆：日历里没有 10-01
        self.assertIsNone(self.query("CN", date(2026, 10, 1)))

        context = FakeQuoteContext.instances[0]
        self.assertEqual(
            context.requests,
            [("2026-10-01", "2026-10-01", "SH.000001")],
        )
        self.assertTrue(context.closed)

    def test_us_calendar_is_independent_of_a_cn_holiday(self):
        FakeQuoteContext.days = [{"time": "2026-10-01", "trade_date_type": "WHOLE"}]
        self.assertEqual(self.query("US", date(2026, 10, 1)), calendar.WHOLE_DAY)

        FakeQuoteContext.days = []
        self.assertIsNone(self.query("CN", date(2026, 10, 1)))

        anchors = [request[2] for context in FakeQuoteContext.instances
                   for request in context.requests]
        self.assertEqual(anchors, ["US.QQQ", "SH.000001"])

    def test_three_markets_keep_independent_holidays(self):
        """2026-10-02：A股国庆休市，港股与美股照常开市 —— 三个锚点各问各的日历"""
        FakeQuoteContext.days_by_symbol = {
            "SH.000001": [],
            "HK.800000": [{"time": "2026-10-02", "trade_date_type": "WHOLE"}],
            "US.QQQ": [{"time": "2026-10-02", "trade_date_type": "WHOLE"}],
        }

        self.assertIsNone(self.query("CN", date(2026, 10, 2)))
        self.assertEqual(self.query("HK", date(2026, 10, 2)), calendar.WHOLE_DAY)
        self.assertEqual(self.query("US", date(2026, 10, 2)), calendar.WHOLE_DAY)
        self.assertEqual(
            [request[2] for context in FakeQuoteContext.instances
             for request in context.requests],
            ["SH.000001", "HK.800000", "US.QQQ"],
        )

    def test_half_day_still_counts_as_a_trading_day(self):
        # 美股感恩节次日、圣诞前夜都是半日市（MORNING）
        FakeQuoteContext.days = [{"time": "2026-11-27", "trade_date_type": "MORNING"}]

        self.assertEqual(self.query("US", date(2026, 11, 27)), calendar.HALF_DAY)
        with patch.dict(sys.modules, {"futu": fake_futu()}):
            self.assertTrue(
                calendar.is_trading_day(
                    "US", date(2026, 11, 27), cache_dir=self.cache_dir
                )
            )

    def test_query_failure_raises_and_still_closes_the_owned_connection(self):
        FakeQuoteContext.ret = -1
        FakeQuoteContext.days = "Futu 交易日查询失败"

        with self.assertRaisesRegex(RuntimeError, "交易日查询失败"):
            self.query("US", date(2026, 11, 27))

        self.assertTrue(FakeQuoteContext.instances[0].closed)


class CalendarCacheTest(TradingCalendarTestBase):
    def test_settled_date_is_answered_from_cache_without_querying(self):
        FakeQuoteContext.days = [{"time": "2026-09-25", "trade_date_type": "WHOLE"}]
        self.assertEqual(self.query("US", date(2026, 9, 25)), calendar.WHOLE_DAY)
        self.assertEqual(len(FakeQuoteContext.instances), 1)

        FakeQuoteContext.days = "不该被问到"
        self.assertEqual(self.query("US", date(2026, 9, 25)), calendar.WHOLE_DAY)
        self.assertEqual(len(FakeQuoteContext.instances), 1)

    def test_today_is_queried_even_when_cached(self):
        today = calendar.today_in("US")
        FakeQuoteContext.days = []
        self.assertIsNone(self.query("US", today))

        FakeQuoteContext.days = [{"time": today.isoformat(), "trade_date_type": "WHOLE"}]
        self.assertEqual(self.query("US", today), calendar.WHOLE_DAY)

    def test_cached_conclusion_covers_a_failing_query(self):
        today = calendar.today_in("US")
        FakeQuoteContext.days = []
        self.assertIsNone(self.query("US", today))

        FakeQuoteContext.ret = -1
        self.assertIsNone(self.query("US", today))

    def test_cache_is_written_per_market(self):
        FakeQuoteContext.days = [{"time": "2026-09-25", "trade_date_type": "WHOLE"}]
        self.query("US", date(2026, 9, 25))

        self.assertTrue((self.cache_dir / "trading_days_us.json").exists())
        self.assertFalse((self.cache_dir / "trading_days_cn.json").exists())


if __name__ == "__main__":
    unittest.main()
