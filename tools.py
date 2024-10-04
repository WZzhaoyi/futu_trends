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

import futu as ft
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import re
import yfinance as yf

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
    elif futu_code.startswith('US'):
        return futu_code.replace('US.', '')
    else:
        assert re.match(r'^[A-Z]{2}.\d{6}$', futu_code)
        return '.'.join(reversed(futu_code.split('.')))

def yfinance_code_to_futu_code(yfinance_code: str) -> str:
    """
        Convert Yahoo Finance Stock Code to Futu Stock Code format
        E.g., 9988.HK -> HK.09988
    :param yfinance_code: Stock code used in Yahoo Finance (e.g., 9988.HK)
    """
    assert re.match(r'^\d{4}.[A-Z]{2}$', yfinance_code)
    if 'HK' in yfinance_code:
        return '.'.join(reversed(('0' + yfinance_code).split('.')))
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

def map_futu_to_yfinance_params(ktype=None, start:datetime=None, end:datetime=None, max_count=None):
    # 映射 ktype 到 interval
    yf_interval_map = {
        'K_1M': '1m', 'K_3M': '2m', 'K_5M': '5m', 'K_15M': '15m',
        'K_30M': '30m', 'K_60M': '60m', 'K_DAY': '1d', 'K_WEEK': '1wk',
        'K_MON': '1mo', 'K_QUARTER': '3mo'
    }

    # 映射 max_count 到 period
    yf_period_map = {
        1: '1d', 5: '5d', 30: '1mo', 90: '3mo', 180: 'ytd', 365: 'ytd',
        730: 'max', 1825: 'max', 3650: 'max'
    }

    yf_params = {}

    if ktype:
        yf_params['interval'] = yf_interval_map.get(ktype, '1d')

    if start:
        yf_params['start'] = datetime.strptime(start, '%Y-%m-%d')

    if end:
        yf_params['end'] = datetime.strptime(end, '%Y-%m-%d')
    if max_count and not (start and end):
        # 如果没有指定 start 和 end，则使用 period
        yf_params['period'] = yf_period_map.get(max_count, '1y')

    return yf_params

def codeInFutuGroup(group_name:str, host='127.0.0.1', port=11111):
    quote_ctx = ft.OpenQuoteContext(host=host, port=port)

    ret, data = quote_ctx.get_user_security(group_name)

    quote_ctx.close()

    if ret == ft.RET_OK:
        return data
    else:
        print('error:', data)

def kline(code:str, max_count:int=365, ktype=ft.KLType.K_DAY, host='127.0.0.1', port=11111):
    
    if 'US' in code:
        if ktype == ft.KLType.K_DAY:
            max_count = 90
        
        kline_seconds = get_kline_seconds(ktype)

        end = datetime.now()
        delta_days = kline_seconds*max_count // (7*60*60)
        start = end - timedelta(days=delta_days)

        stock_code = futu_code_to_yfinance_code(code)
        param = map_futu_to_yfinance_params(ktype=ktype, start=start_str, end=end_str)
        history = yf.Ticker(stock_code).history(**param)

        if history.empty:
            return pd.Series(), pd.Series(), pd.Series()

        df = history[['Open', 'Close', 'Volume', 'High', 'Low']]
        # Create a Date column
        # df['Date'] = df.index
        # Drop the Date as index
        df.reset_index(drop=True, inplace=True)

        return df.High, df.Low, df.Close
    
    if 'HK' in code or 'SH' in code or 'SZ' in code:

        kline_seconds = get_kline_seconds(ktype)

        end = datetime.now()
        delta_days = kline_seconds*max_count // (5*60*60)
        start = end - timedelta(days=delta_days)
        start_str = start.strftime('%Y-%m-%d')
        end_str = end.strftime('%Y-%m-%d')
         
        quote_ctx = ft.OpenQuoteContext(host=host, port=port)
        kline = quote_ctx.request_history_kline(code, ktype=ktype, max_count=max_count, start=start_str, end=end_str)

        quote_ctx.close()

        if kline[0] != ft.RET_OK:
            return pd.Series(), pd.Series(), pd.Series()

        high = kline[1]['high']
        low = kline[1]['low']
        close = kline[1]['close']

        return high, low, close

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

if __name__ == "__main__":
    code = 'SH.000922'
    
    high, low, close = kline(code)

    print(RSI(close))
    print(AO(high,low))