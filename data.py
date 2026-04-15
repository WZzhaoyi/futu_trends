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
#  Written by Joey <wzzhaoyi@outlook.com>, 2026
#  Copyright (c)  Joey - All Rights Reserved

import configparser
from time import sleep
import time as time_module
import futu as ft
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import yfinance as yf
from tools import futu_code_to_yfinance_code, futu_code_to_longbridge_code
import math
import akshare as ak
import threading
import os
import json
import re
import requests
import glob as glob_module

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
_longbridge_lock = threading.Lock()

# 全局代理配置
_proxy_configured = False
_proxy_url = None
_global_session = None

# ---- 市场状态缓存 ----

_state_cache = None
_state_ts = 0
_STATE_TTL = 300  # 5 分钟

_MARKET_KEY_MAP = {
    'SH': 'market_sh', 'SZ': 'market_sz',
    'HK': 'market_hk', 'US': 'market_us',
}
_TRADING_STATES = {
    'MORNING', 'AFTERNOON', 'NIGHT', 'NIGHT_OPEN',
    'PRE_MARKET_BEGIN', 'AFTER_HOURS_BEGIN',
    'AUCTION', 'HK_CAS', 'TRADE_AT_LAST',
}

def _is_trading(code, host, port):
    global _state_cache, _state_ts
    now = int(time_module.time())
    if not _state_cache or now - _state_ts >= _STATE_TTL:
        ctx = ft.OpenQuoteContext(host=host, port=port)
        try:
            ret, data = ctx.get_global_state()
            if ret == ft.RET_OK:
                _state_cache = data
                _state_ts = int(data.get('timestamp', now))
        finally:
            ctx.close()
    market = code.split('.')[0].upper()
    key = _MARKET_KEY_MAP.get(market)
    if not key or not _state_cache:
        return True  # 未知市场或获取失败，保守当作盘中
    return _state_cache.get(key, 'NONE') in _TRADING_STATES

# ---- K线缓存 ----

_kline_cache = {}  # (code, ktype) → (fetch_ts, df)
_KLINE_TTL_TRADING = 60  # 盘中 60 秒

def _is_cache_fresh(fetch_ts, df, max_count, code, config):
    if len(df) < max_count:
        return False
    host = config.get("CONFIG", "FUTU_HOST", fallback="127.0.0.1")
    port = int(config.get("CONFIG", "FUTU_PORT", fallback=11111))
    if not _is_trading(code, host, port):
        return True  # 不在交易，缓存永远有效
    return int(time_module.time()) - fetch_ts < _KLINE_TTL_TRADING

def _find_cache_file(cache_dir, code, ktype):
    pattern = os.path.join(cache_dir, f'data_{code.replace(".", "_")}_{ktype}_*.csv')
    files = glob_module.glob(pattern)
    if not files:
        return None, 0
    latest = max(files, key=lambda f: int(f.rsplit('_', 1)[1].split('.')[0]))
    fetch_ts = int(latest.rsplit('_', 1)[1].split('.')[0])
    return latest, fetch_ts

def _write_cache_file(cache_dir, code, ktype, df):
    os.makedirs(cache_dir, exist_ok=True)
    fetch_ts = int(time_module.time())
    pattern = os.path.join(cache_dir, f'data_{code.replace(".", "_")}_{ktype}_*.csv')
    for old_file in glob_module.glob(pattern):
        os.remove(old_file)
    new_file = os.path.join(cache_dir, f'data_{code.replace(".", "_")}_{ktype}_{fetch_ts}.csv')
    df.to_csv(new_file)
    return fetch_ts

def setup_global_proxy(proxy_url: str = None):
    """
    设置全局代理，使所有 HTTP 请求（包括 yfinance）都走代理
    类似 Windows 系统中的自动设置系统代理
    
    Args:
        proxy_url: 代理地址，格式如 'http://127.0.0.1:7890'
                   如果为 None，则从环境变量读取
    """
    global _proxy_configured, _proxy_url, _global_session
    
    if proxy_url:
        _proxy_url = proxy_url
    else:
        # 从环境变量读取
        _proxy_url = os.environ.get('HTTP_PROXY') or os.environ.get('HTTPS_PROXY')
    
    if not _proxy_url:
        return
    
    # 方法1: 设置环境变量（requests 库会自动读取）
    os.environ['HTTP_PROXY'] = _proxy_url
    os.environ['HTTPS_PROXY'] = _proxy_url
    os.environ['http_proxy'] = _proxy_url
    os.environ['https_proxy'] = _proxy_url
    
    # 方法2: Monkey patch requests 的默认 session
    # 创建一个带代理的 session 并替换 requests 的默认方法
    class ProxiedSession(requests.Session):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.proxies = {
                'http': _proxy_url,
                'https': _proxy_url
            }
            # 不配置重试策略：一旦陷入流控或代理失效，重试毫无意义
    
    # 保存原始的 requests 方法
    if not hasattr(requests, '_original_get'):
        requests._original_get = requests.get
        requests._original_post = requests.post
        requests._original_request = requests.request
        requests._original_session = requests.Session
    
    # 创建全局 session 实例
    _global_session = ProxiedSession()
    
    # 替换 requests 的全局方法
    def proxied_get(url, **kwargs):
        if 'proxies' not in kwargs:
            kwargs['proxies'] = {'http': _proxy_url, 'https': _proxy_url}
        return _global_session.get(url, **kwargs)
    
    def proxied_post(url, **kwargs):
        if 'proxies' not in kwargs:
            kwargs['proxies'] = {'http': _proxy_url, 'https': _proxy_url}
        return _global_session.post(url, **kwargs)
    
    def proxied_request(method, url, **kwargs):
        if 'proxies' not in kwargs:
            kwargs['proxies'] = {'http': _proxy_url, 'https': _proxy_url}
        return _global_session.request(method, url, **kwargs)
    
    requests.get = proxied_get
    requests.post = proxied_post
    requests.request = proxied_request
    requests.Session = ProxiedSession
    
    _proxy_configured = True


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

def convert_to_Nhour(df: pd.DataFrame, n_hours: int = 4, session_type: str = '') -> pd.DataFrame:
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

def fetch_futu_data(code: str, ktype: str, max_count: int, config: configparser.ConfigParser) -> pd.DataFrame | None:
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

def fetch_yfinance_data(code: str, ktype: str, max_count: int, config: configparser.ConfigParser) -> pd.DataFrame | None:
    """获取YFinance最近数据"""
    global _proxy_configured
    
    with _yfinance_lock:
        # 设置全局代理（如果尚未配置）
        if not _proxy_configured:
            proxy = config.get("CONFIG", "PROXY", fallback=None)
            if proxy:
                setup_global_proxy(proxy)
        
        sleep(1)
            
        yf_code = futu_code_to_yfinance_code(code)
        params = get_yfinance_params(ktype, max_count)
        
        tic = yf.Ticker(yf_code)

        history = tic.history(**params)
        if history.empty:
            return None
        df = history[['Open', 'Close', 'Volume', 'High', 'Low']].copy()
        
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

def fetch_akshare_data(code: str, ktype: str, max_count: int) -> pd.DataFrame | None:
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

def fetch_longbridge_data(code: str, ktype: str, max_count: int, config: configparser.ConfigParser) -> pd.DataFrame | None:
    """获取Longbridge最近数据"""
    from longbridge.openapi import QuoteContext, Config, Period, AdjustType

    period_map = {
        'K_1M': Period.Min_1, 'K_5M': Period.Min_5, 'K_15M': Period.Min_15,
        'K_30M': Period.Min_30, 'K_60M': Period.Min_60,
        'K_DAY': Period.Day, 'K_WEEK': Period.Week, 'K_MON': Period.Month,
    }

    with _longbridge_lock:
        # 确定实际请求的K线类型和数量
        _ktype = ktype
        request_count = max_count
        if ktype in ['K_HALF_DAY', 'K_240M', 'K_120M']:
            _ktype = 'K_60M'
            multiplier = {'K_HALF_DAY': 6, 'K_240M': 4, 'K_120M': 2}.get(ktype, 1)
            request_count = min(1000, max_count * multiplier)

        period = period_map.get(_ktype, Period.Day)
        symbol = futu_code_to_longbridge_code(code)

        # 环境变量优先；未设置时从 config.ini 补位
        for key in ("LONGBRIDGE_APP_KEY", "LONGBRIDGE_APP_SECRET", "LONGBRIDGE_ACCESS_TOKEN"):
            if not os.environ.get(key):
                val = config.get("CONFIG", key, fallback=None)
                if val:
                    os.environ[key] = val

        lb_config = Config.from_apikey_env()
        ctx = QuoteContext(lb_config)

        candlesticks = ctx.history_candlesticks_by_offset(
            symbol=symbol,
            period=period,
            adjust_type=AdjustType.ForwardAdjust,
            forward=False,
            count=request_count,
        )

        if not candlesticks:
            return None

        records = []
        for c in candlesticks:
            records.append({
                'time_key': c.timestamp,
                'open': float(c.open),
                'high': float(c.high),
                'low': float(c.low),
                'close': float(c.close),
                'volume': int(c.volume),
            })

        df = pd.DataFrame(records)
        df['time_key'] = pd.to_datetime(df['time_key'])
        df = df.set_index('time_key')
        df = df.sort_index().tail(request_count)

        sleep(0.5)

        return df

def get_kline_data(code: str, config: configparser.ConfigParser, max_count: int = 270, file_cache_dir: str | None = None) -> pd.DataFrame | None:
    """
    统一的K线获取接口，支持内存缓存和可选的文件缓存。
    缓存过期由市场交易状态决定：盘中 60s TTL，盘后永不过期。

    Args:
        code (str): 股票代码
        config (configparser.ConfigParser): 配置对象
        max_count (int): 获取K线的数量
        file_cache_dir (str): 文件缓存目录，None 则仅使用内存缓存

    Returns:
        pd.DataFrame: K线数据，包含 open, high, low, close, volume
    """
    ktype = config.get("CONFIG", "FUTU_PUSH_TYPE")
    cache_key = (code, ktype)

    # 第一层：内存缓存
    if cache_key in _kline_cache:
        fetch_ts, cached_df = _kline_cache[cache_key]
        if _is_cache_fresh(fetch_ts, cached_df, max_count, code, config):
            return cached_df.tail(max_count).copy()

    # 第二层：文件缓存
    if file_cache_dir:
        file_path, fetch_ts = _find_cache_file(file_cache_dir, code, ktype)
        if file_path:
            df = pd.read_csv(file_path, index_col=0, parse_dates=True)
            if _is_cache_fresh(fetch_ts, df, max_count, code, config):
                _kline_cache[cache_key] = (fetch_ts, df)
                return df.tail(max_count).copy()

    # 第三层：远程拉取
    market = code.split('.')[0].upper()
    market_key = f"DATA_SOURCE_{market}"
    if config.has_option("CONFIG", market_key):
        source_type = config.get("CONFIG", market_key).lower()
    else:
        source_type = config.get("CONFIG", "DATA_SOURCE").lower()

    if source_type == 'futu':
        df = fetch_futu_data(code, ktype, max_count, config)
    elif source_type == 'yfinance':
        df = fetch_yfinance_data(code, ktype, max_count, config)
    elif source_type == 'akshare':
        df = fetch_akshare_data(code, ktype, max_count)
    elif source_type == 'longbridge':
        df = fetch_longbridge_data(code, ktype, max_count, config)
    else:
        raise ValueError("Unsupported data source")

    if df is None or df.empty:
        return None

    # 确保时间索引
    df.index = pd.to_datetime(df.index)

    # 处理特殊周期
    if ktype == 'K_HALF_DAY':
        df = convert_to_Nhour(df, session_type='HALF_DAY')
    elif ktype == 'K_240M':
        df = convert_to_Nhour(df, n_hours=4).dropna()
    elif ktype == 'K_120M':
        df = convert_to_Nhour(df, n_hours=2).dropna()

    # 写缓存
    if df is not None and not df.empty:
        fetch_ts = int(time_module.time())
        _, old = _kline_cache.get(cache_key, (0, pd.DataFrame()))
        if len(df) >= len(old):
            _kline_cache[cache_key] = (fetch_ts, df)
        if file_cache_dir:
            _write_cache_file(file_cache_dir, code, ktype, df)

    return df

if __name__ == "__main__":
    import time as t
    from ft_config import get_config

    config = get_config('config_template.ini')
    code = 'HK.00700'
    cache_dir = './data/test_cache'

    # 测试远程拉取 + 缓存写入
    print('=== 远程拉取 ===')
    t0 = t.time()
    df = get_kline_data(code, config, max_count=100, file_cache_dir=cache_dir)
    print(f'{len(df)} 条, {t.time()-t0:.2f}s')

    # 测试内存缓存命中（不同 max_count）
    print('\n=== 内存缓存 ===')
    t0 = t.time()
    df = get_kline_data(code, config, max_count=50, file_cache_dir=cache_dir)
    print(f'{len(df)} 条, {t.time()-t0:.2f}s')

    # 测试市场状态
    host = config.get("CONFIG", "FUTU_HOST")
    port = int(config.get("CONFIG", "FUTU_PORT"))
    print(f'\n=== 市场状态 ===')
    print(f'HK trading: {_is_trading(code, host, port)}')