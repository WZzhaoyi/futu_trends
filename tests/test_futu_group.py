import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

import pandas as pd


class FutuGroupResultTest(unittest.TestCase):
    def test_price_reminder_failure_makes_sync_fail(self):
        context = mock.Mock()
        context.get_user_security.return_value = (0, pd.DataFrame({"code": []}))
        context.modify_user_security.return_value = (0, "ok")
        context.set_price_reminder.side_effect = [
            (1, "delete failed"),
            (0, "up ok"),
            (0, "down ok"),
        ]
        futu = types.SimpleNamespace(
            RET_OK=0,
            OpenQuoteContext=mock.Mock(return_value=context),
            ModifyUserSecurityOp=types.SimpleNamespace(
                MOVE_OUT="move_out",
                ADD="add",
            ),
            SetPriceReminderOp=types.SimpleNamespace(
                DEL_ALL="delete_all",
                ADD="add",
            ),
            PriceReminderType=types.SimpleNamespace(
                PRICE_UP="price_up",
                PRICE_DOWN="price_down",
            ),
            PriceReminderFreq=types.SimpleNamespace(ONCE="once"),
        )
        path = Path(__file__).resolve().parents[1] / "futu_group.py"
        spec = importlib.util.spec_from_file_location("futu_group_under_test", path)
        module = importlib.util.module_from_spec(spec)

        with mock.patch.dict(sys.modules, {"futu": futu}):
            spec.loader.exec_module(module)
            with self.assertLogs(module.logger, level="ERROR"):
                result = module.sync_futu_group(
                    "signals",
                    ["US.TEST"],
                    price_up_list=[2.0],
                    price_down_list=[1.0],
                    overwrite=False,
                    reminder_sleep_seconds=0,
                )

        self.assertFalse(result)
        context.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
