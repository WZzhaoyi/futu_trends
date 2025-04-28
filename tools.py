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
        if re.match(r'^[A-Z]{2}.\d{5}$', futu_code) and futu_code[3] == '0':
            return '.'.join(reversed(futu_code.split('.')))[1:]
        return '.'.join(reversed(futu_code.split('.')))
    elif futu_code.startswith('US.'):
        return futu_code.replace('US.', '')
    elif futu_code.startswith('SH.'):
        return '.'.join(reversed(futu_code.split('.'))).replace('SH', 'SS')
    else:
        assert re.match(r'^[A-Z]{2}.\d{6}$', futu_code)
        return '.'.join(reversed(futu_code.split('.')))

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

    yf_params = {'back_adjust':False}

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

def crossover_status(series_a: pd.Series, series_b: pd.Series) -> list:
    """判断series_a对series_b的上穿、下穿或不相交状态。
    返回: [1: 上穿, -1: 下穿, 0: 不相交]"""
    if len(series_a) != len(series_b):
        raise ValueError("两个Series必须等长")
    status = [0]  # 第一个位置无前值
    for i in range(1, len(series_a)):
        if series_a.iloc[i-1] <= series_b.iloc[i-1] and series_a.iloc[i] > series_b.iloc[i]:
            status.append(1)  # 上穿
        elif series_a.iloc[i-1] >= series_b.iloc[i-1] and series_a.iloc[i] < series_b.iloc[i]:
            status.append(-1)  # 下穿
        else:
            status.append(0)  # 不相交
    return status

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
        tuple: (K, D, J)
    """
    low_min = low.rolling(window=N).min()
    high_max = high.rolling(window=N).max()
    
    RSV = 100 * (close - low_min) / (high_max - low_min)
    
    K = SMA(RSV,M1)
    D = SMA(K,M2)
    J = 3 * K - 2 * D
    
    return K, D, J

def MACD(close: pd.Series, fast_period=12, slow_period=26, signal_period=9):
    """
    计算MACD的DIF和DEA

    Args:
        close (pd.Series): 收盘价序列
        fast_period (int): 快速移动平均线的周期
        slow_period (int): 慢速移动平均线的周期
        signal_period (int): 信号线的周期

    Returns:
        tuple: (DIF, DEA)
    """
    ema_fast = close.ewm(span=fast_period, adjust=False).mean()
    ema_slow = close.ewm(span=slow_period, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal_period, adjust=False).mean()
    return dif, dea

def detect_divergence(indicator_a: pd.Series, indicator_b: pd.Series, price: pd.Series, 
                      golden_crosses: list = None, dead_crosses: list = None) -> pd.Series:
    """通用背离检测函数，接受预计算的交叉点"""
    divergence = pd.Series(0, index=price.index)
    idxs = price.index.to_list()

    # 获取交叉状态并提取金叉和死叉索引
    if golden_crosses is None or dead_crosses is None:
        crossover = crossover_status(indicator_a, indicator_b)
        golden_crosses = [i for i, c in enumerate(crossover) if c == 1]  # 金叉索引
        dead_crosses = [i for i, c in enumerate(crossover) if c == -1]  # 死叉索引

    # 顶背离检测
    for i in range(1, len(dead_crosses)):
        last_dc, prev_dc = dead_crosses[-i], dead_crosses[-i-1]
        gc = next((g for g in reversed(golden_crosses) if prev_dc < g < last_dc), None)
        if gc:
            max_price_curr = price.iloc[gc:last_dc+1].max()
            max_a_curr = indicator_a.iloc[gc:last_dc+1].max()
            prev_gc = next((g for g in reversed(golden_crosses) if g < prev_dc), None)
            if prev_gc:
                max_price_prev = price.iloc[prev_gc:prev_dc+1].max()
                max_a_prev = indicator_a.iloc[prev_gc:prev_dc+1].max()
                if max_price_curr > max_price_prev and max_a_curr < max_a_prev:
                    divergence.loc[idxs[last_dc]] = 1  # 标记顶背离

    # 底背离检测
    for i in range(1, len(golden_crosses)):
        last_gc, prev_gc = golden_crosses[-i], golden_crosses[-i-1]
        dc = next((d for d in reversed(dead_crosses) if prev_gc < d < last_gc), None)
        if dc:
            min_price_curr = price.iloc[dc:last_gc+1].min()
            min_a_curr = indicator_a.iloc[dc:last_gc+1].min()
            prev_dc = next((d for d in reversed(dead_crosses) if d < prev_gc), None)
            if prev_dc:
                min_price_prev = price.iloc[prev_dc:prev_gc+1].min()
                min_a_prev = indicator_a.iloc[prev_dc:prev_gc+1].min()
                if min_price_curr < min_price_prev and min_a_curr > min_a_prev:
                    divergence.loc[idxs[last_gc]] = -1  # 标记底背离

    return divergence

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
def calc_momentum(close: pd.Series, N=21, method='linear')->pd.Series:
    """
    计算动量因子序列
    返回一个包含所有时间点动量值的Series
    """
    result = pd.Series(index=close.index, dtype=float)
    
    # 对于每个时间点，计算从该点往前N个周期的动量
    for i in range(N-1, len(close)):
        price = close.iloc[i-N+1:i+1].tolist()
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
            ma_short = close.rolling(window=10).mean().iloc[i]
            ma_long = close.rolling(window=50).mean().iloc[i]
            score = ma_short - ma_long
        elif method == 'kendall':
            # 保留原有的kendall方法实现
            pass
        else:
            score = 0
            
        result.iloc[i] = score
    
    # 填充前N-1个位置为NaN
    result.iloc[:N-1] = np.nan
    
    return result

if __name__ == "__main__":
    code = 'SH.510880'
    
    df = kline(code)

    high = df['high']  # 从 DataFrame 中提取 high 列
    low = df['low']    # 从 DataFrame 中提取 low 列
    close = df['close']  # 从 DataFrame 中提取 close 列

    print(RSI(close))
    print(AO(high,low))
    print(KDJ(close,high,low))
    print(calc_momentum(close))
    dif,dea = MACD(close)
    print(detect_divergence(dif,dea,close).tail(60))