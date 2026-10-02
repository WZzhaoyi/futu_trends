import configparser
import importlib.util
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pandas as pd


MODULE_PATH = Path(__file__).resolve().parents[1] / "trends.py"
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("trends_tested", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
trends = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = trends
SPEC.loader.exec_module(trends)


def make_config(market="CN", **extra):
    config = configparser.ConfigParser()
    config["CONFIG"] = {
        "FUTU_HOST": "127.0.0.1",
        "FUTU_PORT": "11111",
        "FUTU_GROUP": "TEST",
        "FUTU_PUSH_TYPE": "K_DAY",
        "FUTU_KEYWORD": "",
        "DATA_SOURCE": "futu",
        "TRADING_DAY_GATE": "on",
        **extra,
    }
    if market is not None:
        config.set("CONFIG", "MARKET", market)
    return config


def trends_frame(*codes):
    rows = {code: [f"{code} 信号", "20260930"] for code in codes}
    return pd.DataFrame(
        {"msg": [row[0] for row in rows.values()], "kline_date": [row[1] for row in rows.values()]},
        index=pd.Index(list(rows), name="futu_code"),
    )


MIXED_FRAME = trends_frame("SZ.159941", "HK.00700", "ZERO_AXIS")


class MarketReportRowsTest(unittest.TestCase):
    """代码前缀→市场由 tools.market_of_code 提供（用例在 test_tools.py）"""

    def test_only_declared_and_closed_markets_lose_their_rows(self):
        frame = trends_frame("SZ.159941", "HK.00700", "US.QQQ", "ZERO_AXIS")

        report_df, skipped = trends.market_report_rows(frame, {"CN"})

        self.assertEqual(skipped, {"CN"})
        # 休市的 CN 行被剔除；HK/US 未声明休市、0轴行照旧保留
        self.assertEqual(list(report_df.index), ["HK.00700", "US.QQQ", "ZERO_AXIS"])


class MainTradingDayGateTest(unittest.TestCase):
    def run_main(self, config, calendar_reply, frame=MIXED_FRAME):
        """跑一次 main()，返回 (退出码, stdout, [(渠道, 正文)], 是否抓过行情)"""
        sent = []
        fetched = []

        class RecordingEngine:
            def __init__(self, config):
                pass

            def send_telegram_message(self, message, *args, **kwargs):
                sent.append(("telegram", message))

            def send_email(self, subject, message, *args, **kwargs):
                sent.append(("email", message))

        output = io.StringIO()
        with (
            patch.object(trends, "get_config", return_value=config),
            patch.object(
                trends.trading_calendar,
                "is_trading_day",
                side_effect=calendar_reply,
            ),
            patch.object(
                trends,
                "code_in_futu_group",
                return_value=pd.DataFrame({"code": ["US.TEST"], "name": ["T"]}),
            ),
            patch.object(
                trends,
                "check_trends",
                side_effect=lambda code_pd, config: fetched.append(code_pd) or frame,
            ),
            patch.object(trends, "NotificationEngine", RecordingEngine),
            patch("rank_rotation.save_snapshot"),
            redirect_stdout(output),
        ):
            code = trends.main()
        return code, output.getvalue(), sent, fetched

    def test_holiday_skips_fetch_and_push(self):
        code, output, sent, fetched = self.run_main(
            make_config("CN"), lambda *a, **kw: False
        )

        self.assertEqual(code, 0)
        self.assertIn("非交易日，跳过信号计算与推送", output)
        self.assertEqual(sent, [])
        self.assertEqual(fetched, [])

    def test_trading_day_reports_as_usual(self):
        code, output, sent, fetched = self.run_main(
            make_config("US"), lambda *a, **kw: True, frame=trends_frame("US.QQQ")
        )

        self.assertEqual(code, 0)
        self.assertNotIn("非交易日", output)
        self.assertEqual([channel for channel, _ in sent], ["telegram", "email"])
        self.assertIn("US.QQQ 信号", sent[0][1])
        self.assertNotIn("休市跳过", sent[0][1])
        self.assertEqual(len(fetched), 1)

    def test_mixed_market_config_reports_only_open_markets(self):
        """A股+港股混合分组：港股开市、A股休市 → 只推港股行并标注跳过"""
        asked = []

        def calendar_reply(market, *args, **kwargs):
            asked.append(market)
            return market == "HK"

        code, output, sent, _ = self.run_main(make_config("CN,HK"), calendar_reply)

        self.assertEqual(code, 0)
        self.assertEqual(asked, ["CN", "HK"])
        self.assertEqual([channel for channel, _ in sent], ["telegram", "email"])
        report = sent[0][1]
        self.assertIn("HK.00700 信号", report)
        self.assertNotIn("SZ.159941 信号", report)
        self.assertIn("休市跳过: CN", report)

    def test_push_is_skipped_when_only_closed_markets_have_rows(self):
        code, output, sent, _ = self.run_main(
            make_config("CN,HK"),
            lambda market, *a, **kw: market == "HK",
            frame=trends_frame("SZ.159941", "ZERO_AXIS"),
        )

        self.assertEqual(code, 0)
        self.assertIn("CN 今日休市，没有可报的标的，跳过推送", output)
        self.assertEqual(sent, [])

    def test_weekly_config_reports_even_on_a_non_trading_day(self):
        """周报类任务（K_WEEK）故意排在周末：闸门关闭，不查日历、不剔行"""

        def calendar_must_not_be_asked(*args, **kwargs):
            raise AssertionError("TRADING_DAY_GATE=off 时不应查询交易日")

        code, output, sent, fetched = self.run_main(
            make_config("CN,HK", TRADING_DAY_GATE="off"),
            calendar_must_not_be_asked,
        )

        self.assertEqual(code, 0)
        self.assertIn("已关闭交易日闸门", output)
        self.assertEqual([channel for channel, _ in sent], ["telegram", "email"])
        report = sent[0][1]
        self.assertIn("SZ.159941 信号", report)
        self.assertIn("HK.00700 信号", report)
        self.assertNotIn("休市跳过", report)
        self.assertEqual(len(fetched), 1)

    def test_mixed_market_config_skips_when_all_closed(self):
        code, output, sent, fetched = self.run_main(
            make_config("CN,HK"), lambda *a, **kw: False
        )

        self.assertEqual(code, 0)
        self.assertIn("CN/HK 非交易日", output)
        self.assertEqual(sent, [])
        self.assertEqual(fetched, [])

    def test_unavailable_calendar_keeps_the_original_flow(self):
        def unavailable(*args, **kwargs):
            raise RuntimeError("OpenD 未启动")

        code, output, sent, _ = self.run_main(make_config("CN"), unavailable)

        self.assertEqual(code, 0)
        self.assertIn("交易日查询失败，按交易日继续", output)
        # 日历不可用时不做任何行过滤，保持原样全量推送
        self.assertEqual([channel for channel, _ in sent], ["telegram", "email"])
        self.assertIn("SZ.159941 信号", sent[0][1])
        self.assertNotIn("休市跳过", sent[0][1])

    def test_missing_market_config_fails_loudly(self):
        with (
            patch.object(trends, "get_config", return_value=make_config(None)),
            patch.object(
                trends,
                "check_trends",
                side_effect=AssertionError("配置错误时不应抓行情"),
            ),
            patch.object(
                trends, "NotificationEngine", side_effect=AssertionError("不应推送")
            ),
            redirect_stdout(io.StringIO()),
        ):
            with self.assertRaisesRegex(ValueError, "缺少 CONFIG/MARKET"):
                trends.main()


if __name__ == "__main__":
    unittest.main()
