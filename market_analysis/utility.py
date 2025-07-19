from asyncio.log import logger

import sqlalchemy
import yfinance as yf
import numpy as np
import pandas_ta as pandas_ta
import pandas as pd
import talib as tb
from io import BytesIO
import requests
import os
import glob

"""
Hong Kong, Shenzhen, Shanghai stocks used number + exch appendix (e.g. HK, SZ, SS)
so used a generator to generate the possible value within a pre-defined range.
"""


def shenzhen_ticker_generator():
    for i in range(1000001, 1003999):
        yield str(i)[1:] + ".SZ"


def techboard_ticker_generator():
    for i in range(1300001, 1301599):
        yield str(i)[1:] + ".SZ"


def b_ticker_generator():
    for i in range(1200002, 1201000):
        # for i in range(1201000,1202000):
        yield str(i)[1:]


def shanghai_ticker_generator():
    for i in range(1600001, 1605999):
        yield str(i)[1:] + ".SS"

def download_excel_from_url(url: str) -> pd.DataFrame:
    """
    从指定URL下载并读取Excel文件内容
    
    Args:
        url (str): Excel文件的下载链接
    
    Returns:
        pd.DataFrame: 读取到的DataFrame对象
    
    Raises:
        requests.RequestException: 当下载失败时抛出
        pd.errors.EmptyDataError: 当Excel文件为空时抛出
    """
    # 发送GET请求下载文件
    response = requests.get(url, timeout=10)
    response.raise_for_status()  # 检查请求是否成功
    
    # 将响应内容转换为Excel文件对象
    file_content = BytesIO(response.content)
    return pd.read_excel(file_content)

# 港股通
def ggt_generator():
    url = "https://www.szse.cn/api/report/ShowReport?SHOWTYPE=xlsx&CATALOGID=SGT_GGTBDQD&TABKEY=tab1&random=0.2204823490345611"
    df = download_excel_from_url(url)
    
    tickers = df['证券代码'].astype(str).apply(
        lambda x: x.zfill(4) + '.HK'
    ).to_list()
    
    for t in tickers:
        yield t

# 科创50
def kc50_generator():
    url = "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/cons/000688cons.xls"
    df = download_excel_from_url(url)
    
    tickers = df['成份券代码Constituent Code'].astype(str).apply(
        lambda x: x.zfill(6) + '.SS' if x.startswith('6') else x.zfill(6) + '.SZ'
    ).to_list()
    
    for t in tickers:
        yield t

# 中证A500
def a500_generator():
    url = "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/cons/000510cons.xls"
    df = download_excel_from_url(url)
    
    tickers = df['成份券代码Constituent Code'].astype(str).apply(
        lambda x: x.zfill(6) + '.SS' if x.startswith('6') else x.zfill(6) + '.SZ'
    ).to_list()
    
    for t in tickers:
        yield t

# 中证500
def zz500_generator():
    url = "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/cons/000905cons.xls"
    df = download_excel_from_url(url)
    
    tickers = df['成份券代码Constituent Code'].astype(str).apply(
        lambda x: x.zfill(6) + '.SS' if x.startswith('6') else x.zfill(6) + '.SZ'
    ).to_list()
    
    for t in tickers:
        yield t

# 沪深300
def hs300_generator():
    url = "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/cons/000300cons.xls"
    df = download_excel_from_url(url)
    
    tickers = df['成份券代码Constituent Code'].astype(str).apply(
        lambda x: x.zfill(6) + '.SS' if x.startswith('6') else x.zfill(6) + '.SZ'
    ).to_list()
    
    for t in tickers:
        yield t

def hk_ticker_generator():
    for i in range(10001, 19999):
        yield str(i)[1:] + ".HK"

# 恒生科技指数
def hktech_ticker_generator():
    table = pd.read_html('https://zh.wikipedia.org/wiki/%E6%81%92%E7%94%9F%E7%A7%91%E6%8A%80%E6%8C%87%E6%95%B8')
    ls = table[1].values[0]
    tickers = []
    for i in ls:
        i = i.split(' ')
        for j in i:
            if type(j) == type('123') and j.isnumeric():
                tickers.append(j[1:] + ".HK")
    for t in tickers:
        yield t

# 恒生指数
def hsi_ticker_generator():
    table = pd.read_html('https://zh.wikipedia.org/wiki/%E6%81%92%E7%94%9F%E6%8C%87%E6%95%B8#%E6%81%92%E7%94%9F%E6%8C%87%E6%95%B8%E6%88%90%E4%BB%BD%E8%82%A1')
    ls = table[14].values
    tickers = []
    for i in ls:
        i = i[0]
        if type(i) == type('123') and i.isnumeric():
            tickers.append(i[1:] + ".HK")
    for t in tickers:
        yield t

# S&P 500
def sp_500_generator():
    table = pd.read_html('https://en.wikipedia.org/wiki/List_of_S%26P_500_companies')
    df = table[0]
    tickers = df.Symbol.to_list()
    for t in tickers:
        yield t

# NASDAQ 100
def nasdaq_100_generator():
    table = pd.read_html('https://en.wikipedia.org/wiki/Nasdaq-100')
    df = table[4]
    tickers = df.Symbol.to_list()
    for t in tickers:
        yield t

def yfinance_to_tdx_ebk(tickers, output_file=None):
    """
    将yfinance格式的股票代码转换为通达信EBK格式
    :param tickers: 单个股票代码字符串或股票代码列表
    :param output_file: 输出的EBK文件路径，如果为None则只返回转换后的列表
    :return: 转换后的股票代码列表
    """
    if isinstance(tickers, str):
        tickers = [tickers]
    
    tdx_codes = []
    for ticker in tickers:
        # 移除可能存在的空格
        ticker = ticker.strip()
        
        # 处理后缀
        if ticker.endswith('.SS'):
            # 上海市场
            code = f"1#{ticker[:-3]}"
        elif ticker.endswith('.SZ'):
            # 深圳市场
            code = f"0#{ticker[:-3]}"
        elif ticker.endswith('.HK'):
            # 香港市场
            code = f"20{ticker[:-3]}"
        else:
            # 对于没有后缀的代码，假设为美股
            code = f"31#{ticker}"
        
        tdx_codes.append(code)
    
    if output_file:
        # 写入EBK文件
        with open(output_file, 'w', encoding='gbk') as f:
            for code in tdx_codes:
                f.write(code + '\n')
        logger.info(f"已将股票代码列表保存到: {output_file}")
    
    return tdx_codes


if __name__ == "__main__":
    a500 = ggt_generator()
    
    print("前10个成分股代码:")
    for _ in range(10):
        try:
            print(next(a500))
        except StopIteration:
            print("已遍历所有成分股")
            break
        except Exception as e:
            print(f"生成器错误: {e}")
            break