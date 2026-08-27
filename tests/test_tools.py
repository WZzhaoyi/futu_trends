import pandas as pd
import unittest
from unittest.mock import patch

import tools


class FakeQuoteContext:
    def __init__(self, ret=tools.ft.RET_OK, data=None):
        self.ret = ret
        self.data = data
        self.requested_code = None
        self.closed = False

    def get_plate_stock(self, code):
        self.requested_code = code
        return self.ret, self.data

    def close(self):
        self.closed = True


class GetConstituentsTest(unittest.TestCase):
    def test_resolves_alias_and_reuses_context(self):
        context = FakeQuoteContext(
            data=pd.DataFrame({"code": ["SH.600000", "SZ.000001"]})
        )

        result = tools.get_constituents("a500", quote_ctx=context)

        self.assertEqual(result, ["SH.600000", "SZ.000001"])
        self.assertEqual(context.requested_code, "SH.000510")
        self.assertFalse(context.closed)

    def test_closes_owned_context(self):
        context = FakeQuoteContext(data=pd.DataFrame({"code": ["HK.00700"]}))
        with patch.object(tools.ft, "OpenQuoteContext", return_value=context):
            result = tools.get_constituents("HK.800000")

        self.assertEqual(result, ["HK.00700"])
        self.assertEqual(context.requested_code, "HK.800000")
        self.assertTrue(context.closed)

    def test_raises_on_futu_error(self):
        context = FakeQuoteContext(ret=tools.ft.RET_ERROR, data="unknown index")

        with self.assertRaisesRegex(RuntimeError, "unknown index"):
            tools.get_constituents("BAD", quote_ctx=context)


if __name__ == "__main__":
    unittest.main()
