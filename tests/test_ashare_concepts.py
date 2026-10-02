import configparser
import importlib.util
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pandas as pd


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "market_analysis"
    / "ashare_concepts.py"
)
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location(
    "ashare_concepts_tested",
    MODULE_PATH,
)
assert SPEC is not None and SPEC.loader is not None
concepts = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = concepts
SPEC.loader.exec_module(concepts)


def make_config():
    config = configparser.ConfigParser()
    config["CONFIG"] = {"FUTU_HOST": "127.0.0.1", "FUTU_PORT": "11111"}
    return config


def empty_frame():
    return pd.DataFrame(columns=["code", "name", "amount", "pct", "concepts"])


class MainTradingDayGateTest(unittest.TestCase):
    def run_main(self, calendar_result, analyze):
        """跑一次 main()，返回 (退出码, stdout, 推送的渠道列表)"""
        sent = []

        class RecordingEngine:
            def __init__(self, config):
                pass

            def send_email(self, subject, msg):
                sent.append("email")

            def send_telegram_message(self, msg):
                sent.append("telegram")

            def send_webhook(self, msg):
                sent.append("webhook")

        output = io.StringIO()
        with (
            patch.object(concepts, "get_config", return_value=make_config()),
            patch.object(concepts, "is_trading_day", side_effect=calendar_result),
            patch.object(concepts, "analyze_ashare_concepts", side_effect=analyze),
            patch.object(concepts, "NotificationEngine", RecordingEngine),
            redirect_stdout(output),
        ):
            code = concepts.main()
        return code, output.getvalue(), sent

    def test_holiday_skips_fetch_sync_and_push(self):
        def refuse(**kwargs):
            raise AssertionError("非交易日不应抓取数据")

        code, output, sent = self.run_main(lambda *a, **kw: False, refuse)

        self.assertEqual(code, 0)
        self.assertIn("非A股交易日", output)
        self.assertEqual(sent, [])

    def test_concept_sync_reads_the_cn_calendar(self):
        """闸门必须问 A 股日历，而不是别的市场"""
        calls = []

        def record(*args, **kwargs):
            calls.append((args, kwargs))
            return False

        self.run_main(record, lambda **kwargs: empty_frame())

        self.assertEqual(calls[0][0], ("CN",))
        self.assertEqual(calls[0][1]["config"].get("CONFIG", "FUTU_HOST"), "127.0.0.1")

    def test_trading_day_reports_as_usual(self):
        code, output, sent = self.run_main(
            lambda *a, **kw: True,
            lambda **kwargs: empty_frame(),
        )

        self.assertEqual(code, 0)
        self.assertNotIn("非A股交易日", output)
        self.assertEqual(sent, ["email", "telegram", "webhook"])

    def test_unavailable_calendar_keeps_the_original_flow(self):
        """交易日历查不到时按交易日继续，避免交易日漏推"""

        def unavailable(*args, **kwargs):
            raise RuntimeError("OpenD 未启动")

        code, output, sent = self.run_main(unavailable, lambda **kwargs: empty_frame())

        self.assertEqual(code, 0)
        self.assertIn("A股交易日查询失败", output)
        self.assertEqual(sent, ["email", "telegram", "webhook"])


if __name__ == "__main__":
    unittest.main()
