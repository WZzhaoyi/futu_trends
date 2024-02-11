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
import datetime
import re
import yfinance as yf
import configparser
import os
from telegram_bot import TelegramBotEngine
from email_engine import EmailEngine


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

def codeInFutuGroup(group_name:str, host='127.0.0.1', port=11111):
    quote_ctx = ft.OpenQuoteContext(host=host, port=port)

    ret, data = quote_ctx.get_user_security(group_name)

    quote_ctx.close()

    if ret == ft.RET_OK:
        return data
    else:
        print('error:', data)

def kline(code:str, period:str="1y"):
    
    if 'US' in code:
        stock_code = futu_code_to_yfinance_code(code)
        history = yf.Ticker(stock_code).history(period=period)

        df = history[['Open', 'Close', 'Volume', 'High', 'Low']]
        # Create a Date column
        # df['Date'] = df.index
        # Drop the Date as index
        df.reset_index(drop=True, inplace=True)

        return df.High, df.Low, df.Close
    
    if 'HK' in code or 'SH' in code or 'SZ' in code:
         
        today = datetime.datetime.now()
        one_year = today - datetime.timedelta(days=365)
        start = one_year.strftime('%Y-%m-%d')
        end = today.strftime('%Y-%m-%d')
        quote_ctx = ft.OpenQuoteContext(host='127.0.0.1', port=11111)
        kline = quote_ctx.request_history_kline(code, end=end, start=start)

        high = kline[1]['high']
        low = kline[1]['low']
        close = kline[1]['close']
        quote_ctx.close()

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

def isReverse(high, low, close):# 最近一根K线创新高/低 收盘价位于K线下半部/上半部 RSI超买/超卖
    last_index = close.size - 1
    last_close = close[last_index]
    last_rsi = RSI(close)[last_index]
    last_ave = (high[last_index]+low[last_index])/2
    if last_close < close[last_index-1] and last_close < close[last_index-2] and last_ave < last_close and last_rsi < 25.0:
         return True
    if last_close > close[last_index-1] and last_close > close[last_index-2] and last_ave > last_close and last_rsi > 75.0:
         return True
    return False

def isContinue(high, low):# 取倒数5根AO柱 近3根趋势与之前相反
    ao = AO(high, low)
    [a1, a2, a3, a4, a5] = ao[-5:]
    if a3 < 0 and a1 > a2 and  a2 < a3 and a3 < a4 and a4 < a5:
         return True
    if a3 > 0 and a1 < a2 and a2 > a3 and a3 > a4 and a4 > a5:
         return True
    return False

def checkTrends(code_in_group):

    trends = []
    if code_in_group.size:
         print('----{} check trends for {}----'.format(datetime.datetime.now(), group))
         print(datetime.datetime.now())
         name_list = code_in_group['name']
         for idx, futu_code in enumerate(code_in_group['code'].values):
            high, low, close = kline(futu_code)
            rev = isReverse(high,low,close) # 趋势反转
            co = isContinue(high,low) # 趋势延续
            name = name_list[idx]
            if rev:
                trends.append('{} {} 趋势反转'.format(futu_code, name, rev))
            if co:
                trends.append('{} {} 趋势延续'.format(futu_code, name, co))
    return trends

if __name__ == "__main__":
    BASE_DIR = os.path.split(os.path.realpath(__file__))[0]
    config = configparser.ConfigParser()
    config.read(os.path.join(BASE_DIR, 'config.ini'), encoding='utf-8')
    host = config.get("CONFIG", "FUTU_HOST")
    port = config.get("CONFIG", "FUTU_PORT")
    group = config.get("CONFIG", "FUTU_GROUP")
    telegram = config.get("CONFIG", "TELEGRAM_BOT_TOKEN")
    emails = config.get("CONFIG", "EMAIL_SUBSCRIBTION").split(',')

    ls = codeInFutuGroup(group,host,int(port))
    trends = checkTrends(ls)
    
    if telegram is not None:
        telebot = TelegramBotEngine()
        telebot.send_telegram_message('{} {}:\n{}'.format(datetime.datetime.now().strftime('%Y-%m-%d'), group, '\n'.join(trends)),'https://www.futunn.com/')

    if len(emails):
        emailWorker = EmailEngine()
        for address in emails:
            emailWorker.send_email(address,group,'<p>{} {}:<br>{}</p>'.format(datetime.datetime.now().strftime('%Y-%m-%d'), group, '<br>'.join(trends)))