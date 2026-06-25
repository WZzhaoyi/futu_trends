"""Index and constituent-list fetch helpers.

This module intentionally keeps only list-fetching utilities. Historical
fundamental screening, local databases, and model pipelines were removed in
favor of the Futu OpenD screeners in this package.
"""

from __future__ import annotations

from io import BytesIO
from typing import Iterable

import pandas as pd
import requests


def download_excel_from_url(url: str, timeout: int = 10) -> pd.DataFrame:
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    return pd.read_excel(BytesIO(response.content))


def _yield_codes(codes: Iterable[str]):
    for code in codes:
        yield code


def ggt_generator():
    """港股通名单。"""
    url = (
        "https://www.szse.cn/api/report/ShowReport?"
        "SHOWTYPE=xlsx&CATALOGID=SGT_GGTBDQD&TABKEY=tab1"
    )
    df = download_excel_from_url(url)
    codes = df["证券代码"].astype(str).apply(lambda x: f"{x.zfill(4)}.HK")
    yield from _yield_codes(codes)


def kc50_generator():
    """科创 50 成分股。"""
    url = "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/cons/000688cons.xls"
    yield from _csindex_generator(url)


def a500_generator():
    """中证 A500 成分股。"""
    url = "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/cons/000510cons.xls"
    yield from _csindex_generator(url)


def zz500_generator():
    """中证 500 成分股。"""
    url = "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/cons/000905cons.xls"
    yield from _csindex_generator(url)


def hs300_generator():
    """沪深 300 成分股。"""
    url = "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/cons/000300cons.xls"
    yield from _csindex_generator(url)


def _csindex_generator(url: str):
    df = download_excel_from_url(url)
    codes = df["成份券代码Constituent Code"].astype(str).apply(
        lambda x: f"{x.zfill(6)}.SS" if x.startswith("6") else f"{x.zfill(6)}.SZ"
    )
    yield from _yield_codes(codes)


def hk_all_ticker_generator():
    """HKEX main-board equity securities list."""
    url = "https://www.hkex.com.hk/eng/services/trading/securities/securitieslists/ListOfSecurities.xlsx"
    df = download_excel_from_url(url, timeout=30)
    rows = df[df["Sub-Category"] == "Equity Securities (Main Board)"]
    codes = rows["Stock Code"].astype(str).apply(lambda x: f"{x.zfill(4)}.HK")
    yield from _yield_codes(codes)


def hktech_ticker_generator():
    """恒生科技指数成分股。"""
    table = pd.read_html("https://zh.wikipedia.org/wiki/%E6%81%92%E7%94%9F%E7%A7%91%E6%8A%80%E6%8C%87%E6%95%B8")
    values = table[1].values[0]
    tickers = []
    for item in values:
        for part in str(item).split(" "):
            if part.isnumeric():
                tickers.append(f"{part[-4:]}.HK")
    yield from _yield_codes(tickers)


def hsi_ticker_generator():
    """恒生指数成分股。"""
    table = pd.read_html(
        "https://zh.wikipedia.org/wiki/%E6%81%92%E7%94%9F%E6%8C%87%E6%95%B8"
        "#%E6%81%92%E7%94%9F%E6%8C%87%E6%95%B8%E6%88%90%E4%BB%BD%E8%82%A1"
    )
    tickers = []
    for row in table[14].values:
        item = str(row[0])
        if item.isnumeric():
            tickers.append(f"{item[-4:]}.HK")
    yield from _yield_codes(tickers)


def sp_500_generator():
    """S&P 500 constituents."""
    df = pd.read_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")[0]
    yield from _yield_codes(df.Symbol.astype(str).tolist())


def nasdaq_100_generator():
    """Nasdaq 100 constituents."""
    df = pd.read_html("https://en.wikipedia.org/wiki/Nasdaq-100")[4]
    yield from _yield_codes(df.Symbol.astype(str).tolist())
