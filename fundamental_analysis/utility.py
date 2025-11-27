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

def hk_all_ticker_generator():
    xlsx_url = 'https://www.hkex.com.hk/eng/services/trading/securities/securitieslists/ListOfSecurities.xlsx'
    
    response = requests.get(xlsx_url, timeout=30, stream=True)
    response.raise_for_status()
    content = BytesIO(response.content)
    
    table = pd.read_excel(content, header=2)
    stock_codes = table[table['Sub-Category'] == 'Equity Securities (Main Board)']['Stock Code'].tolist()
    for t in stock_codes:
        ticker_code = str(t).zfill(4)
        yield ticker_code + ".HK"

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

def get_data(tickers):
    data = []
    for ticker in tickers:
        data.append((ticker, yf.download(ticker).reset_index()))
    return data


def create_engine(name):
    """
    创建 SQLite 数据库连接
    :param name: 可以是数据库名称或完整路径
    :return: SQLAlchemy engine 对象
    """
    # 检查是否是完整路径
    if '/' in name or '\\' in name:
        # 如果是完整路径，直接使用
        connection_string = f"sqlite:///{name}"
    else:
        # 如果只是数据库名，在后面加上 .db 后缀
        connection_string = f"sqlite:///{name}.db"
        
    engine = sqlalchemy.create_engine(connection_string)
    return engine


def TOSQL(frames, engine):
    for symbol, frame in frames:
        frame.to_sql(symbol, engine.raw_connection(), index=False, if_exists="replace")
    logger.info("imported successfully")


def check_pandas(df: pd.DataFrame):
    logger.info("Process started for function: get_return..")
    if df.empty:
        logger.debug("The dataframe is empty. No transformations will be applied.")
        return df
    logger.info("Applying transformations to pd")


def get_return(df: pd.DataFrame):
    """
    :param data_frame: Pandas DataFrame as Input

    :returns:
    data_frame: Transformed Pandas DataFrame as Output
    """
    check_pandas(df)
    # also could just be df["Close"].pct_change()
    df["Return"] = round(df["Close"] / df["Close"].shift(1) - 1, 4)
    logger.info("Process completed for function: get_return..")
    return df


def get_state(df: pd.DataFrame):
    """
    :param data_frame: Pandas DataFrame as Input

    :returns:
    data_frame: Transformed Pandas DataFrame as Output
    """
    check_pandas(df)
    df["state"] = np.where(df["Return"] > 0, "1", "0")
    return df


def get_garman_klass_vol(df):
    """
    :param data_frame: Pandas DataFrame as Input

    :returns:
    data_frame: Transformed Pandas DataFrame as Output
    """
    check_pandas(df)
    df["garman_klass_vol"] = ((np.log(df["High"]) - np.log(df["Low"])) ** 2) / 2 - (
            2 * np.log(2) - 1
    ) * ((np.log(df["Close"]) - np.log(df["Open"])) ** 2)
    logger.info("Process completed for function: get_garman_klass_vol..")
    return df


def get_rsi(df):
    """
    :param data_frame: Pandas DataFrame as Input

    :returns:
    data_frame: Transformed Pandas DataFrame as Output
    """
    check_pandas(df)
    df["rsi"] = df["Close"].transform(lambda x: pandas_ta.rsi(close=x, length=20))
    logger.info("Process completed for function: get_rsi..")
    return df


def get_bb_low(df):
    """
    :param data_frame: Pandas DataFrame as Input

    :returns:
    data_frame: Transformed Pandas DataFrame as Output
    """
    check_pandas(df)
    df["bb_low"] = df["Close"].transform(
        lambda x: pandas_ta.bbands(close=np.log1p(x), length=20).iloc[:, 0]
    )
    # normalize the value by dividing it against adjust close price)
    df["bb_low"] = df["bb_low"] / df["Close"]
    return df


def get_bb_mid(df):
    """
    :param data_frame: Pandas DataFrame as Input

    :returns:
    data_frame: Transformed Pandas DataFrame as Output
    """
    check_pandas(df)
    df["bb_mid"] = df["Close"].transform(
        lambda x: pandas_ta.bbands(close=np.log1p(x), length=20).iloc[:, 1]
    )
    # normalize the value by dividing it against adjust close price)
    df["bb_mid"] = df["bb_mid"] / df["Close"]
    return df


def get_bb_high(df):
    """
    :param data_frame: Pandas DataFrame as Input

    :returns:
    data_frame: Transformed Pandas DataFrame as Output
    """
    check_pandas(df)
    df["bb_high"] = df["Close"].transform(
        lambda x: pandas_ta.bbands(close=np.log1p(x), length=20).iloc[:, 2]
    )
    # normalize the value by dividing it against adjust close price)
    df["bb_high"] = df["bb_high"] / df["Close"]
    return df


def compute_atr(stock_data):
    atr = pandas_ta.atr(
        high=stock_data["High"],
        low=stock_data["Low"],
        close=stock_data["Close"],
        length=14,
    )
    return atr.sub(atr.mean()).div(atr.std())


def get_atr(df):
    """
    :param data_frame: Pandas DataFrame as Input

    :returns:
    data_frame: Transformed Pandas DataFrame as Output
    """
    check_pandas(df)
    df["atr"] = df.apply(compute_atr)
    return df


def compute_macd(close):
    """
    :param data_frame: Pandas DataFrame as Input

    :returns:
    data_frame: Transformed Pandas DataFrame as Output
    """
    macd = pandas_ta.macd(close=close, length=20).iloc[:, 0]
    return macd.sub(macd.mean()).div(macd.std())


def get_macd(df):
    """
    :param data_frame: Pandas DataFrame as Input

    :returns:
    data_frame: Transformed Pandas DataFrame as Output
    """
    df["macd"] = df["Close"].apply(compute_macd)
    return df


def get_dollar_volume(df):
    """
    :param data_frame: Pandas DataFrame as Input

    :returns:
    data_frame: Transformed Pandas DataFrame as Output
    """
    df["dollar_volume"] = (df["Close"] * df["Volume"]) / 1e6
    return df


def get_technical_analysis_features(df, ticker):
    """
    use talib to get tech indicators directory
    """
    o = df['Open'].values
    c = df['Close'].values
    h = df['High'].values
    l = df['Low'].values
    v = df['Volume'].astype(float).values
    # define the technical analysis matrix

    df['MA5'] = tb.MA(c, timeperiod=5)
    df['MA10'] = tb.MA(c, timeperiod=10)
    df['MA20'] = tb.MA(c, timeperiod=20)
    df['MA60'] = tb.MA(c, timeperiod=60)
    df['MA120'] = tb.MA(c, timeperiod=120)
    df['MA5'] = tb.MA(v, timeperiod=5)
    df['MA10'] = tb.MA(v, timeperiod=10)
    df['MA20'] = tb.MA(v, timeperiod=20)
    df['ADX'] = tb.ADX(h, l, c, timeperiod=14)
    df['ADXR'] = tb.ADXR(h, l, c, timeperiod=14)
    df['MACD'] = tb.MACD(c, fastperiod=12, slowperiod=26, signalperiod=9)[0]
    df['RSI'] = tb.RSI(c, timeperiod=14)
    df['BBANDS_U'] = tb.BBANDS(c, timeperiod=5, nbdevup=2, nbdevdn=2, matype=0)[0]
    df['BBANDS_M'] = tb.BBANDS(c, timeperiod=5, nbdevup=2, nbdevdn=2, matype=0)[1]
    df['BBANDS_L'] = tb.BBANDS(c, timeperiod=5, nbdevup=2, nbdevdn=2, matype=0)[2]
    df['AD'] = tb.AD(h, l, c, v)
    df['ATR'] = tb.ATR(h, l, c, timeperiod=14)
    df['HT_DC'] = tb.HT_DCPERIOD(c)

    return df

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

def process_stock_data(ticker_list_path: str, database_path: str) -> None:
    """
    从CSV文件读取股票列表，获取数据并存入数据库
    
    Args:
        ticker_list_path (str): 股票列表CSV文件的完整路径
        database_path (str): 数据库保存路径
        
    Returns:
        None
        
    Example:
        process_stock_data(
            ticker_list_path="/path/to/stocklist.csv",
            database_path="/path/to/database/stocks.db"
        )
    """
    try:
        # 直接读取指定的 CSV 文件
        if not os.path.exists(ticker_list_path):
            logger.error(f"找不到CSV文件: {ticker_list_path}")
            return
            
        # 读取 CSV 文件
        df = pd.read_csv(ticker_list_path)
        target_list = df['Ticker'].tolist()
        
        if not target_list:
            logger.warning("股票列表为空")
            return
            
        # 获取数据并存入数据库
        data = get_data(target_list)
        data_engine = create_engine(database_path)
        TOSQL(data, data_engine)
        
        logger.info(f"成功处理 {len(target_list)} 只股票的数据")

        return data_engine, database_path
        
    except Exception as e:
        logger.error(f"处理股票数据时发生错误: {str(e)}")
        raise

if __name__ == "__main__":
    code_generator = hk_all_ticker_generator()
    
    print("前10个成分股代码:")
    for _ in range(10):
        try:
            print(next(code_generator))
        except StopIteration:
            print("已遍历所有成分股")
            break
        except Exception as e:
            print(f"生成器错误: {e}")
            break