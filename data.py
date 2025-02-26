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
#  Written by Joey <wzzhaoyi@outlook.com>, 2024
#  Copyright (c)  Joey - All Rights Reserved

import configparser
from time import sleep
import futu as ft
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import yfinance as yf
from datetime import datetime
from longport.openapi import QuoteContext, Config, AdjustType
from tools import convert_to_Nhour, futu_code_to_longport_code, futu_code_to_yfinance_code, get_kline_seconds, map_futu_to_longport_params, map_futu_to_yfinance_params\

def get_kline(code:str, config: configparser.ConfigParser, max_count:int=250, autype=ft.AuType.QFQ):
    data_source = config.get("CONFIG", "DATA_SOURCE")
    ktype = config.get("CONFIG", "FUTU_PUSH_TYPE")

    end = datetime.now()
    kline_seconds = get_kline_seconds(ktype)
    if kline_seconds < 24 * 60 * 60:
        # Convert calendar days to business days (approximately)
        bdays = kline_seconds*max_count // (5*60*60)
        # Use pandas bdate_range to get the start date by counting back business days
        start = pd.bdate_range(end=end, periods=bdays, freq='B')[0]
    else:
        # For daily or above periods, calculate business days directly
        start = pd.bdate_range(end=end, periods=max_count, freq='B')[0]

    if data_source == 'yfinance':
        
        stock_code = futu_code_to_yfinance_code(code)
        param = map_futu_to_yfinance_params(ktype=ktype, start=start, end=end)
        history = yf.download(code, **param)

        if history.empty:
            return None

        df = history[['Open', 'Close', 'Volume', 'High', 'Low']].copy()

        # Create a Date column
        # df['time_key'] = df.index
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

        sleep(1)

        return df
    
    elif data_source == 'futu':

        host = config.get("CONFIG", "FUTU_HOST")
        port = int(config.get("CONFIG", "FUTU_PORT"))

        if ktype == 'K_240M' or ktype == 'K_120M':
            _ktype = ft.KLType.K_60M
        else:
            _ktype = ktype

        start_str = start.strftime('%Y-%m-%d')
        end_str = end.strftime('%Y-%m-%d')
         
        quote_ctx = ft.OpenQuoteContext(host=host, port=port)
        kline = quote_ctx.request_history_kline(code, ktype=_ktype, start=start_str, end=end_str,autype=autype)

        quote_ctx.close()

        if kline[0] != ft.RET_OK:
            print(kline[1])
            return None
        
        # 将time_key列转换为DatetimeIndex
        kline[1]['time_key'] = pd.to_datetime(kline[1]['time_key'])
        kline[1].set_index(pd.DatetimeIndex(kline[1]['time_key']), inplace=True)
        
        if ktype == 'K_240M':
            return convert_to_Nhour(kline[1])
        elif ktype == 'K_120M':
            return convert_to_Nhour(kline[1],2).dropna()

        return kline[1]
    
    elif data_source == 'longport':
        app_key = config.get("CONFIG", "LONGPORT_KEY")
        app_secret = config.get("CONFIG", "LONGPORT_SECRET")
        access_token = config.get("CONFIG", "LONGPORT_TOKEN")
        _config = Config(app_key, app_secret, access_token)
        
        stock_code = futu_code_to_longport_code(code)
        param = map_futu_to_longport_params(ktype=ktype)

        ctx = QuoteContext(_config)
        resp = ctx.candlesticks(stock_code, param.period, max_count, AdjustType.ForwardAdjust)
        df = pd.DataFrame(resp)
        
        # 确保时间列是DatetimeIndex
        if 'time' in df.columns:
            df['time'] = pd.to_datetime(df['time'])
            df.set_index(pd.DatetimeIndex(df['time']), inplace=True)
        
        if ktype == 'K_240M':
            df = convert_to_Nhour(df).dropna()
        elif ktype == 'K_120M':
            df = convert_to_Nhour(df,2).dropna()
        
        return df
    
    else:
        raise ValueError(
                "Fail to get data source. Please check inputs for yfinance, futu, longport."
            )