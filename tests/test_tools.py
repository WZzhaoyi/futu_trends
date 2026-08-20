import pandas as pd
import pytest

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


def test_get_constituents_resolves_alias_and_reuses_context():
    context = FakeQuoteContext(
        data=pd.DataFrame({"code": ["SH.600000", "SZ.000001"]})
    )

    result = tools.get_constituents("a500", quote_ctx=context)

    assert result == ["SH.600000", "SZ.000001"]
    assert context.requested_code == "SH.000510"
    assert context.closed is False


def test_get_constituents_closes_owned_context(monkeypatch):
    context = FakeQuoteContext(data=pd.DataFrame({"code": ["HK.00700"]}))
    monkeypatch.setattr(tools.ft, "OpenQuoteContext", lambda **kwargs: context)

    assert tools.get_constituents("HK.800000") == ["HK.00700"]
    assert context.requested_code == "HK.800000"
    assert context.closed is True


def test_get_constituents_raises_on_futu_error():
    context = FakeQuoteContext(ret=tools.ft.RET_ERROR, data="unknown index")

    with pytest.raises(RuntimeError, match="unknown index"):
        tools.get_constituents("BAD", quote_ctx=context)
