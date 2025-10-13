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
from tools import futu_code_to_yfinance_code
import math
import akshare as ak
import threading
import os
import json
import re

# 美股代码列表缓存
_us_stocks_cache = None
# 数据保存目录
_DATA_DIR = './data'
# 缓存有效期（天）
_CACHE_EXPIRE_DAYS = 30

# 添加锁机制 确保同一时间只有一个调用
_futu_lock = threading.Lock()
_yfinance_lock = threading.Lock()
_akshare_lock = threading.Lock()

def get_us_stocks():
    """获取美股代码列表，使用缓存避免重复调用"""
    global _us_stocks_cache
    
    # 如果内存缓存存在，直接返回
    if _us_stocks_cache is not None:
        return _us_stocks_cache
    
    # 准备缓存文件路径
    cache_file = os.path.join(_DATA_DIR, 'us_stocks.json')
    os.makedirs(_DATA_DIR, exist_ok=True)
    
    # 检查缓存文件是否存在且未过期
    if os.path.exists(cache_file):
        file_mtime = datetime.fromtimestamp(os.path.getmtime(cache_file))
        if datetime.now() - file_mtime < timedelta(days=_CACHE_EXPIRE_DAYS):
            with open(cache_file, 'r', encoding='utf-8') as f:
                _us_stocks_cache = pd.read_json(f)
            return _us_stocks_cache
    
    # 如果缓存不存在或已过期，重新获取数据
    _us_stocks_cache = ak.stock_us_spot_em()
    
    # 保存到缓存文件
    with open(cache_file, 'w', encoding='utf-8') as f:
        _us_stocks_cache.to_json(f, force_ascii=False, orient='records')
    
    return _us_stocks_cache

def convert_to_Nhour(df: pd.DataFrame, n_hours: int = 4, session_type: str = None) -> pd.DataFrame:
    """
    将数据转换为N小时周期或半日的K线
    
    Args:
        df (pd.DataFrame): 输入数据，必须包含OHLCV数据，索引为时间戳
        n_hours (int): 目标小时数，默认4小时
        session_type (str): 时间段类型，可选值：
            - None: 按照固定小时数聚合
            - 'HALF_DAY': 按照上午/下午分段聚合
    
    Returns:
        pd.DataFrame: 转换后的K线数据
    """
    # 确保索引是时间类型
    df.index = pd.to_datetime(df.index)
    
    if session_type == 'HALF_DAY':
        # 准备结果数据结构
        result_data = []
        time_map = {'AM': '12:00:00', 'PM': '16:00:00'}
        
        # 获取所有唯一的日期
        dates = pd.Series(df.index.date).unique()
        
        for date in dates:
            # 获取当日数据
            day_data = df[df.index.date == date]
            
            # 分别处理上午和下午的数据
            for session in ['AM', 'PM']:
                if session == 'AM':
                    session_data = day_data[day_data.index.hour < 13]
                else:
                    session_data = day_data[day_data.index.hour >= 13]
                
                # 如果该时段有数据，则生成K线
                if not session_data.empty:
                    kline = {
                        'open': session_data['open'].iloc[0],
                        'high': session_data['high'].max(),
                        'low': session_data['low'].min(),
                        'close': session_data['close'].iloc[-1],
                        'volume': session_data['volume'].sum(),
                        'time_key': pd.to_datetime(f"{date} {time_map[session]}")
                    }
                    result_data.append(kline)
        
        # 创建结果DataFrame
        if len(result_data):
            result = pd.DataFrame(result_data)
            result.set_index('time_key', inplace=True)
            return result
        return pd.DataFrame()
        
    else:
        # 按N小时周期分组
        result = df.groupby(pd.Grouper(freq=f'{n_hours}H')).agg({
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'volume': 'sum'
        })
    
    return result.dropna()  # 删除没有数据的时段

def fetch_futu_data(code: str, ktype: str, max_count: int, config: configparser.ConfigParser) -> pd.DataFrame:
    """获取Futu最近数据"""
    with _futu_lock:
        host = config.get("CONFIG", "FUTU_HOST")
        port = int(config.get("CONFIG", "FUTU_PORT"))
        
        # 确定实际请求的K线类型和数量
        _ktype = ktype
        request_count = max_count
        if ktype in ['K_HALF_DAY', 'K_240M', 'K_120M']:
            _ktype = ft.KLType.K_60M
            multiplier = {'K_HALF_DAY': 6, 'K_240M': 4, 'K_120M': 2}.get(ktype, 1)
            request_count = min(1000, max_count * multiplier)
        
        # 计算时间范围
        end = datetime.now()
        # 根据请求数量和K线类型计算合适的开始时间
        if _ktype == ft.KLType.K_60M:
            # 按交易时段估算，一天约6个交易小时
            days_needed = math.ceil(request_count / 6)
            # 使用交易日历获取开始日期
            start = pd.bdate_range(end=end, periods=days_needed, freq='B')[0]
        else:
            # 对于日K及以上周期，直接用交易日历
            start = pd.bdate_range(end=end, periods=request_count, freq='B')[0]
        
        start_str = start.strftime('%Y-%m-%d')
        end_str = end.strftime('%Y-%m-%d')
        
        quote_ctx = ft.OpenQuoteContext(host=host, port=port)
        try:
            kline = quote_ctx.request_history_kline(
                code=code,
                ktype=_ktype,
                start=start_str,
                end=end_str,
                autype=ft.AuType.QFQ,
                max_count=None
            )
            
            if kline[0] != ft.RET_OK:
                print(f"Futu API error: {kline[1]}")
                return None
                
            df = kline[1].copy()
            df['time_key'] = pd.to_datetime(df['time_key'])
            df = df.set_index('time_key')
            
            # 确保获取最近的数据
            df = df.sort_index().tail(request_count)

            sleep(0.5)
            
            return df
        finally:
            quote_ctx.close()

def get_yfinance_params(ktype: str, max_count: int) -> dict:
    """获取YFinance参数的纯函数"""
    interval_map = {
        'K_1M': '1m', 'K_5M': '5m', 'K_15M': '15m',
        'K_30M': '30m', 'K_60M': '60m', 'K_DAY': '1d',
        'K_WEEK': '1wk', 'K_MON': '1mo'
    }
    
    interval = interval_map.get(ktype, '60m') if ktype not in ['K_240M', 'K_120M', 'K_HALF_DAY'] else '60m'
    
    # 计算适当的时间范围
    if interval.endswith('m') or interval == '1h':
        period = '60d'
    elif interval == '1d':
        period = '5y' if max_count > 730 else '2y' if max_count > 365 else '1y' if max_count > 180 else '6mo'
    elif interval == '1wk':
        period = '10y' if max_count > 260 else '5y' if max_count > 52 else '1y'
    else:  # 月线
        period = '10y' if max_count > 60 else '5y' if max_count > 12 else '1y'
    
    return {'interval': interval, 'period': period}

def fetch_yfinance_data(code: str, ktype: str, max_count: int, config: configparser.ConfigParser) -> pd.DataFrame:
    """获取YFinance最近数据"""
    with _yfinance_lock:
        # 设置代理
        proxy = config.get("CONFIG", "PROXY", fallback=None)
        if proxy:
            yf.set_config(proxy=proxy)
            
        yf_code = futu_code_to_yfinance_code(code)
        params = get_yfinance_params(ktype, max_count)
        
        tic = yf.Ticker(yf_code)

        history = tic.history(**params)
        if history.empty:
            return None
        df = history[['Open', 'Close', 'Volume', 'High', 'Low']].copy()
        
        sleep(1)
        
        return df.rename(columns={
            'Open': 'open', 'High': 'high',
            'Low': 'low', 'Close': 'close',
            'Volume': 'volume'
        })

def get_akshare_params(ktype: str, max_count: int) -> dict:
    """获取AKShare参数的纯函数"""
    # AKShare主要支持日线及以上周期
    period_map = {
        'K_DAY': 'daily',
        'K_WEEK': 'weekly',
        'K_MON': 'monthly'
    }
    
    period = period_map.get(ktype)

    if period is None:
        raise ValueError(f"Unsupported ktype: {ktype}")
    
    # 计算适当的时间范围
    end = datetime.now()
    if period == 'daily':
        # 日线数据，根据请求数量计算开始日期
        start = pd.bdate_range(end=end, periods=max_count, freq='B')[0]
    elif period == 'weekly':
        # 周线数据
        start = end - timedelta(days=max_count * 7)
    else:  # 月线
        start = end - timedelta(days=max_count * 30)
    
    return {
        'period': period,
        'start_date': start.strftime('%Y%m%d'),
        'end_date': end.strftime('%Y%m%d'),
        'adjust': 'qfq'
    }

def fetch_akshare_data(code: str, ktype: str, max_count: int) -> pd.DataFrame:
    """获取AKShare最近数据"""
    with _akshare_lock:
        raw_code = code.split('.')[1]
        params = get_akshare_params(ktype, max_count)
        
        if code.startswith('SH.') or code.startswith('SZ.'):
            if re.match(r'^(51|15|56|58)', raw_code):
                # A股ETF数据
                df = ak.fund_etf_hist_em(symbol=raw_code, **params)
            elif raw_code.startswith('16'):
                # A股LOF数据
                df = ak.fund_lof_hist_em(symbol=raw_code, **params)
            else:
                # A股数据
                df = ak.stock_zh_a_hist(symbol=raw_code, **params)
        elif code.startswith('HK.'):
            # 港股数据
            df = ak.stock_hk_hist(symbol=raw_code, **params)
        elif code.startswith('US.'):
            # 美股数据
            # 获取东财美股代码
            us_stocks = get_us_stocks()
            matched_stock = us_stocks[us_stocks['代码'].str.split('.').str[1] == raw_code]
            if matched_stock.empty:
                return None
            full_symbol = matched_stock.iloc[0]['代码']
            df = ak.stock_us_hist(symbol=full_symbol, **params)
        else:
            raise ValueError(f"Unsupported market code: {code}")
        
        if df.empty:
            return None
        
        # 重命名列以匹配统一格式
        df = df.rename(columns={
            '日期': 'time_key',
            '开盘': 'open',
            '收盘': 'close',
            '最高': 'high',
            '最低': 'low',
            '成交量': 'volume'
        })
        
        # 设置时间索引
        df['time_key'] = pd.to_datetime(df['time_key'])
        df = df.set_index('time_key')
        
        # 确保获取最近的数据
        df = df.sort_index().tail(max_count)
        
        sleep(0.5)
        
        return df

def get_kline_data(code: str, config: configparser.ConfigParser, max_count: int = 270) -> pd.DataFrame:
    """
    统一的K线获取接口
    
    Args:
        code (str): 股票代码
        config (configparser.ConfigParser): 配置对象
        max_count (int): 获取K线的数量
        
    Returns:
        pd.DataFrame: K线数据，包含 open, high, low, close, volume
    """
    source_type = config.get("CONFIG", "DATA_SOURCE").lower()
    ktype = config.get("CONFIG", "FUTU_PUSH_TYPE")
    
    # 获取原始数据
    if source_type == 'futu':
        df = fetch_futu_data(code, ktype, max_count, config)
    elif source_type == 'yfinance':
        df = fetch_yfinance_data(code, ktype, max_count, config)
    elif source_type == 'akshare':
        df = fetch_akshare_data(code, ktype, max_count)
    else:
        raise ValueError("Unsupported data source")
    
    if df is None or df.empty:
        return None
        
    # 确保时间索引
    df.index = pd.to_datetime(df.index)
    
    # 处理特殊周期
    if ktype == 'K_HALF_DAY':
        return convert_to_Nhour(df, session_type='HALF_DAY')
    elif ktype == 'K_240M':
        return convert_to_Nhour(df, n_hours=4).dropna()
    elif ktype == 'K_120M':
        return convert_to_Nhour(df, n_hours=2).dropna()
            
    return df

if __name__ == "__main__":
    # 测试配置
    config = configparser.ConfigParser()
    config.read('config_template.ini',encoding='utf-8')
    
    # 测试不同数据源
    test_cases = [
        ('HK.00700', 'K_60M', 100),    # 腾讯1小时K线
        ('HK.00700', 'K_HALF_DAY', 100), # 腾讯半日K线
        ('US.AAPL', 'K_60M', 100),      # 苹果1小时K线
        ('SH.000001', 'K_DAY', 250),     # 上证日K
    ]
    
    for code, ktype, count in test_cases:
        print(f"\n测试 {code} {ktype} {count}根K线:")
        # 设置K线类型
        config.set("CONFIG", "FUTU_PUSH_TYPE", ktype)
        
        # 测试Futu数据源
        config.set("CONFIG", "DATA_SOURCE", "futu")
        df = get_kline_data(code, config, count)
        print(f"Futu数据源获取到 {len(df) if df is not None else 0} 根K线")
        
        # 测试YFinance数据源
        config.set("CONFIG", "DATA_SOURCE", "yfinance")
        df = get_kline_data(code, config, count)
        print(f"YFinance数据源获取到 {len(df) if df is not None else 0} 根K线")
        
        # 测试AKShare数据源（仅支持日线及以上周期）
        if ktype in ['K_DAY', 'K_WEEK', 'K_MON']:
            config.set("CONFIG", "DATA_SOURCE", "akshare")
            df = get_kline_data(code, config, count)
            print(f"AKShare数据源获取到 {len(df) if df is not None else 0} 根K线")