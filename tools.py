#  Futu Trends
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
#  Written by Joey <wzzhaoyi@outlook.com>, 2023
#  Copyright (c)  Joey - All Rights Reserved

import math
import futu as ft
import pandas as pd
import numpy as np
from typing import Union
from scipy import stats
from datetime import datetime, timedelta
import re
import yfinance as yf
from longport.openapi import Period

def get_kline_seconds(k_type:str): #根据K_1M,K_5M,K_15M,K_30M,K_60M时间含义输出秒数
    if k_type == 'K_DAY':
        return 24 * 60 * 60  # 一天的秒数
    elif k_type == 'K_WEEK':
        return 7 * 24 * 60 * 60  # 一周的秒数
    elif k_type == 'K_MON':
        return 30 * 24 * 60 * 60  # 约一个月的秒数（这是一个近似值）
    elif k_type == 'K_QUARTER':
        return 91 * 24 * 60 * 60  # 约一个季度的秒数（这是一个近似值）
    elif k_type == 'K_YEAR':
        return 365 * 24 * 60 * 60  # 约一年的秒数（这是一个近似值）
    else:
        try:
            return int(k_type.split('_')[1][:-1]) * 60
        except ValueError:
            raise ValueError(f"Unsupported k_type: {k_type}")

def futu_code_to_yfinance_code(futu_code: str) -> str:
    """
        Convert Futu Stock Code to Yahoo Finance Stock Code format
        E.g., HK.09988 -> 9988.HK; US.SOHO -> SOHO
    :param futu_code: Stock code used in Futu (e.g., HK.09988)
    """
    if futu_code.startswith("HK"):
        assert re.match(r'^[A-Z]{2}.\d{5}$', futu_code)
        return '.'.join(reversed(futu_code.split('.')))[1:]
    elif futu_code.startswith('US.'):
        return futu_code.replace('US.', '')
    elif futu_code.startswith('SH.'):
        return '.'.join(reversed(futu_code.split('.'))).replace('SH', 'SS')
    else:
        assert re.match(r'^[A-Z]{2}.\d{6}$', futu_code)
        return '.'.join(reversed(futu_code.split('.')))
    
def futu_code_to_longport_code(futu_code: str) -> str:
    """
        Convert Futu Stock Code to Longport Stock Code format
        E.g., HK.09988 -> 9988.HK; US.SOHO -> SOHO.US
    :param futu_code: Stock code used in Futu (e.g., HK.09988)
    """
    return '.'.join(reversed(futu_code.split('.')))

def yfinance_code_to_futu_code(yfinance_code: str) -> str:
    """
        Convert Yahoo Finance Stock Code to Futu Stock Code format
        E.g., 9988.HK -> HK.09988
    :param yfinance_code: Stock code used in Yahoo Finance (e.g., 9988.HK)
    """
    if 'HK' in yfinance_code:
        return '.'.join(reversed(('0' + yfinance_code).split('.')))
    if 'SS' in yfinance_code:
        return '.'.join(reversed((yfinance_code).split('.'))).replace('SS','SH')
    else:
        return '.'.join(reversed((yfinance_code).split('.')))

def map_history_params(period=None, interval=None, start=None, end=None):
    # 映射 period
    futu_period_map = {
        '1d': 1, '5d': 5, '1mo': 30, '3mo': 90, '6mo': 180, '1y': 365,
        '2y': 730, '5y': 1825, '10y': 3650, 'ytd': None, 'max': None
    }
    
    # 映射 interval
    futu_interval_map = {
        '1m': 'K_1M', '2m': 'K_3M', '5m': 'K_5M', '15m': 'K_15M',
        '30m': 'K_30M', '60m': 'K_60M', '90m': 'K_60M', '1h': 'K_60M',
        '1d': 'K_DAY', '5d': 'K_WEEK', '1wk': 'K_WEEK', '1mo': 'K_MON',
        '3mo': 'K_QUARTER'
    }

    futu_params = {}

    if period:
        futu_params['max_count'] = futu_period_map.get(period)
    
    if interval:
        futu_params['ktype'] = futu_interval_map.get(interval, 'K_DAY')
    
    if start:
       futu_params['start'] = start.strftime('%Y-%m-%d')
    
    if end:
        futu_params['end'] = end.strftime('%Y-%m-%d')

    return futu_params 

def map_futu_to_yfinance_params(ktype:ft.KLType=None, start:datetime=None, end:datetime=None, max_count=None):
    # 映射 ktype 到 interval
    yf_interval_map = {
        'K_1M': '1m', 'K_3M': '2m', 'K_5M': '5m', 'K_15M': '15m',
        'K_30M': '30m', 'K_60M': '60m', 'K_120M': '60m', 'K_240M': '60m', 'K_DAY': '1d', 'K_WEEK': '1wk',
        'K_MON': '1mo', 'K_QUARTER': '3mo'
    }

    # 映射 max_count 到 period
    yf_period_map = {
        1: '1d', 5: '5d', 30: '1mo', 90: '3mo', 180: 'ytd', 365: 'ytd',
        730: 'max', 1825: 'max', 3650: 'max'
    }

    yf_params = {'back_adjust':True}

    if ktype:
        yf_params['interval'] = yf_interval_map.get(ktype, None)

    if start:
        yf_params['start'] = start

    if end:
        yf_params['end'] = end

    if max_count:
        # 如果没有指定 start 和 end，则使用 period
        yf_params['period'] = yf_period_map.get(max_count, None)

    return yf_params

def map_futu_to_longport_params(ktype:ft.KLType=None, start:datetime=None, end:datetime=None, count:int=None, direction=None):
    """
    将 Futu 参数映射到 Longport 的历史 K 线查询参数

    :param ktype: K 线类型
    :param start: 开始日期
    :param end: 结束日期
    :param count: 查询数量
    :param direction: 查询方向
    :return: Longport 查询参数字典
    """
    # 映射 ktype 到 period
    longport_period_map = {
        'K_1M': Period.Min_1, 'K_5M': Period.Min_5, 'K_15M': Period.Min_15,
        'K_30M': Period.Min_30, 'K_60M': Period.Min_60, 'K_120M': Period.Min_60, 'K_240M': Period.Min_60, 'K_DAY': Period.Day, 'K_WEEK': Period.Week,
        'K_MON': Period.Month
    }

    longport_params = {}

    if ktype:
        longport_params['period'] = longport_period_map.get(ktype, None)  # 直接映射 K 线类型

    if start:
        longport_params['start'] = start

    if end:
        longport_params['end'] = end

    if count:
        longport_params['count'] = count

    return longport_params

def code_in_futu_group(group_name:str, host='127.0.0.1', port=11111):
    quote_ctx = ft.OpenQuoteContext(host=host, port=port)

    ret, data = quote_ctx.get_user_security(group_name)

    quote_ctx.close()

    if ret == ft.RET_OK:
        return data
    else:
        print('error:', data)

def convert_to_Nhour(df: pd.DataFrame,hour:2|4=4) -> pd.DataFrame:
    """
    将 60m 数据转换为 2h/4h 数据

    Args:
        df (pd.DataFrame): 包含 60m 数据的 DataFrame，必须包含 'open', 'high', 'low', 'close', 'volume' 列

    Returns:
        pd.DataFrame: 转换后的 2h/4h 数据
    """
    # 确保索引是时间类型
    df.index = pd.to_datetime(df.index)

    # 确保数据长度是hour的倍数
    if len(df) % hour != 0:
        df = df.iloc[(len(df) % hour)-len(df):]  # 截断到4的倍数

    # 初始化一个空的 DataFrame 用于存储数据
    _df = pd.DataFrame(columns=['open', 'high', 'low', 'close', 'volume'])

    # 使用 for 循环遍历每一行
    for i in range(0, len(df), hour):
        index = df.index[i+hour-1]
        _df.loc[index, 'open'] = df.iloc[i]['open']  # 开盘价
        _df.loc[index, 'high'] = df.iloc[i:i+hour]['high'].max()  # 最高价
        _df.loc[index, 'low'] = df.iloc[i:i+hour]['low'].min()  # 最低价
        _df.loc[index, 'close'] = df.iloc[i+hour-1]['close']  # 收盘价
        _df.loc[index, 'volume'] = df.iloc[i:i+hour]['volume'].sum()  # 成交量

    # 复制索引为新的一列 time_key
    _df['time_key'] = _df.index

    return _df

def kline(code:str, max_count:int=365, ktype=ft.KLType.K_DAY, host='127.0.0.1', port=11111, autype=ft.AuType.QFQ):
    if ktype == ft.KLType.K_DAY:
        max_count = 90

    if 'US' in code:
        
        kline_seconds = get_kline_seconds(ktype)

        end = datetime.now()
        delta_days = kline_seconds*max_count // (7*60*60)
        start = end - timedelta(days=delta_days)

        stock_code = futu_code_to_yfinance_code(code)
        param = map_futu_to_yfinance_params(ktype=ktype, start=start, end=end)
        history = yf.Ticker(stock_code).history(**param)

        if history.empty:
            return None

        df = history[['Open', 'Close', 'Volume', 'High', 'Low']].copy()

        # Create a Date column
        df['time_key'] = df.index
        # Drop the Date as index
        # df.reset_index(drop=True, inplace=True)

        df.rename(columns={
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Open": "open",
            "Volume": "volume"
        }, inplace=True)

        if ktype == 'K_240M':
            df = convert_to_Nhour(df).dropna()
        elif ktype == 'K_120M':
            df = convert_to_Nhour(df,2).dropna()

        return df
    
    if 'HK' in code or 'SH' in code or 'SZ' in code:

        if ktype == 'K_240M' or ktype == 'K_120M':
            _ktype = ft.KLType.K_60M
        else:
            _ktype = ktype

        kline_seconds = get_kline_seconds(_ktype)

        end = datetime.now()
        delta_days = kline_seconds*max_count // (5*60*60)
        start = end - timedelta(days=delta_days)
        start_str = start.strftime('%Y-%m-%d')
        end_str = end.strftime('%Y-%m-%d')
         
        quote_ctx = ft.OpenQuoteContext(host=host, port=port)
        kline = quote_ctx.request_history_kline(code, ktype=_ktype, start=start_str, end=end_str,autype=autype)

        quote_ctx.close()

        if kline[0] != ft.RET_OK:
            print(kline[1])
            return None
        
        if ktype == 'K_240M':
            return convert_to_Nhour(kline[1])
        elif ktype == 'K_120M':
            df = convert_to_Nhour(kline[1],2).dropna()

        return kline[1]

def RD(N,D=3):   
	return np.round(N,D)

def AVE(H, L):         #两序列对应元素average
    return (H+L)/2

def MAX(S1,S2):        #序列max
	return np.maximum(S1,S2)

def ABS(S):      
	return np.abs(S)

def REF(S, N=1):       #对序列整体下移动N,返回序列(shift后会产生NAN)    
    return pd.Series(S).shift(N).values

def MA(S,N):           #求序列的N日平均值，返回序列                    
    return pd.Series(S).rolling(N).mean().values

def EMA(S,N):          #指数移动平均,为了精度 S>4*N  EMA至少需要120周期       
    return pd.Series(S).ewm(span=N, adjust=False).mean().values

def SMA(S, N, M=1):    #中国式的SMA,至少需要120周期才精确 (雪球180周期)    alpha=1/(1+com)
    return pd.Series(S).ewm(com=N-M, adjust=True).mean().values   

def AO(H, L):
    M =AVE(H, L)
    return MA(M,5)-MA(M,34)

def RSI(CLOSE, N=24):
    DIF = CLOSE-REF(CLOSE,1) 
    return RD(SMA(MAX(DIF,0), N) / SMA(ABS(DIF), N) * 100)  

def KDJ(close: pd.Series, high: pd.Series, low: pd.Series, N=15, M1=5, M2=5) -> pd.DataFrame:
    """
    计算 KDJ 指标

    Args:
        close (pd.Series): 收盘价序列
        high (pd.Series): 最高价序列
        low (pd.Series): 最低价序列
        N (int): 计算 K 和 D 的周期
        M1 (int): K 的平滑周期
        M2 (int): D 的平滑周期

    Returns:
        pd.DataFrame: 包含 K, D, J 的 DataFrame
    """
    low_min = low.rolling(window=N).min()
    high_max = high.rolling(window=N).max()
    
    RSV = 100 * (close - low_min) / (high_max - low_min)
    
    K = SMA(RSV,M1)
    D = SMA(K,M2)
    J = 3 * K - 2 * D
    
    return pd.DataFrame({'K': K, 'D': D, 'J': J})

def siegelslopes_ma(price_ser: Union[pd.Series, np.ndarray],method:str="hierarchical") -> float:
    """Repeated Median (Siegel 1982)

    Args:
        price_ser (Union[pd.Series, np.ndarray]): index-date values-price or values-price

    Returns:
        float: float
    """
    n: int = len(price_ser)
    res = stats.siegelslopes(price_ser, np.arange(n), method=method)
    return res.intercept + res.slope * (n-1)

def calc_icu_ma(price:pd.Series,N:int)->pd.Series:
    """计算ICU均线

    Args:
        price (pd.Series): index-date values-price
        N (int): 计算窗口
    Returns:
        pd.Series: index-date values-icu_ma
    """
    if len(price) <= N:
        raise ValueError("price length must be greater than N")
    
    return price.rolling(N).apply(siegelslopes_ma,raw=True)

# 动量因子
def calc_momentum(close: pd.Series, N=21, method='linear'):
    price = close.iloc[-N:].tolist()
    y = np.log(price)
    x = np.arange(len(y))
    if method == 'linear':
        slope, intercept = np.polyfit(x, y, 1)
        annualized_returns = math.pow(math.exp(slope), 250) - 1
        r_squared = 1 - (sum((y - (slope * x + intercept))**2) / ((len(y) - 1) * np.var(y, ddof=1)))
        score = annualized_returns * r_squared
    elif method == 'polynomial':
      slope_list = np.polyfit(x, y, 2)
      annualized_returns = math.pow(math.exp(slope_list[1]), 250) - 1
      y_predict = np.polyval(slope_list,x)
      r_squared = 1 - (sum((y-y_predict)**2) / ((len(y) - 1) * np.var(y, ddof=1)))
      score = annualized_returns * r_squared
    elif method == 'ma':
        ma_short = df.close.rolling(window=10).mean().iloc[-1]
        ma_long = df.close.rolling(window=50).mean().iloc[-1]
        score = ma_short - ma_long
    elif method == 'kendall':
        tau, _ = stats.kendalltau(x, y)
        score = tau
    return score

if __name__ == "__main__":
    code = 'SH.000922'
    
    df = kline(code)

    high = df['high']  # 从 DataFrame 中提取 high 列
    low = df['low']    # 从 DataFrame 中提取 low 列
    close = df['close']  # 从 DataFrame 中提取 close 列

    print(RSI(close))
    print(AO(high,low))
    print(KDJ(close,high,low,))
    print(calc_momentum(close))