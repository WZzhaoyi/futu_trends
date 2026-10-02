"""Parameterized indicators and session-aligned bars for the order engine.

Intraday inputs are minute bars labelled by their opening time in market local time.
Windows restart after every lunch break. Short session tails are real bars.
"""
from copy import deepcopy
from datetime import timedelta
import math

import pandas as pd

from data import get_kline_data, convert_to_Nhour, hour_windows
from market_analysis.trading_calendar import market_of, trading_sessions
from signal_analysis.defaults import DEFAULT_PARAMS
import tools

TIMEFRAMES = {'1h': 60, '2h': 120, '3h': 180, '4h': 240, '1d': None, '1w': None}
KLINE_TIMEFRAMES = {'K_60M': '1h', 'K_120M': '2h', 'K_180M': '3h',
                     'K_240M': '4h', 'K_DAY': '1d', 'K_WEEK': '1w'}
TOOL_PARAMS = {
    'RSI': {'N': 24}, 'KDJ': {'N': 15, 'M1': 5, 'M2': 5},
    'MACD': {'fast_period': 12, 'slow_period': 26, 'signal_period': 9},
    'MA': {'N': 20}, 'EMA': {'N': 20},
}
OUTPUTS = {'RSI': (), 'MA': (), 'EMA': (), 'KDJ': ('K', 'D', 'J'),
           'MACD': ('DIF', 'DEA', 'HIST')}
SIGNAL_TYPES = {'top_down': 'RSI', 'reverse': 'KD', 'continue': 'MACD', 'breakout': 'SR'}
SIGNAL_PARAMS = {name: dict(DEFAULT_PARAMS[kind]) for name, kind in SIGNAL_TYPES.items()}
SIGNAL_PARAMS['balance'] = {'M': 5, 'N': 28, 'R': 20}
PERIOD_PARAMS = {'N', 'M', 'M1', 'M2', 'R', 'rsi_period', 'k_period', 'd_period',
                 'fast_period', 'slow_period', 'signal_period', 'lookback', 'pivot_window', 'min_touches'}


def timeframe_of(node, config):
    raw = node.get('timeframe', config.get('CONFIG', 'FUTU_PUSH_TYPE', fallback='K_DAY'))
    result = KLINE_TIMEFRAMES.get(raw, raw)
    if result not in TIMEFRAMES:
        raise ValueError(f'不支持的timeframe: {raw}，可用: {", ".join(TIMEFRAMES)}')
    return result


def validate_indicator(node):
    variable = node['variable']
    namespace, name = variable.split('.', 1)
    definitions = TOOL_PARAMS if namespace == 'tools' else SIGNAL_PARAMS
    if name not in definitions:
        raise ValueError(f'不支持的指标: {variable}')
    params = node.get('params', {})
    if not isinstance(params, dict) or set(params) - set(definitions[name]):
        raise ValueError(f'{variable} params包含未支持参数，可用: {list(definitions[name])}')
    for key, value in params.items():
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
            raise ValueError(f'{variable}.{key} 必须是有限数值')
        if key in PERIOD_PARAMS:
            if int(value) != value or not 1 <= value <= 1000:
                raise ValueError(f'{variable}.{key} 必须是1到1000的整数')
        elif value < 0:
            raise ValueError(f'{variable}.{key} 不能小于0')
    merged = {**definitions[name], **params}
    if 'oversold' in merged:
        for key in ('oversold', 'overbought'):
            if key in params and not 0 <= params[key] <= 100:
                raise ValueError(f'{variable}.{key} 必须在0到100之间')
        if {'oversold', 'overbought'} <= set(params) and not (
                merged['oversold'] <= merged['overbought']
                and (name != 'top_down' or merged['oversold'] < merged['overbought'])):
            raise ValueError(f'{variable} oversold必须低于overbought（KD允许相等）')
    if ('fast_period' in merged and (namespace == 'tools' or
            {'fast_period', 'slow_period'} <= set(params))
            and merged['fast_period'] >= merged['slow_period']):
        raise ValueError(f'{variable} fast_period必须小于slow_period')
    if namespace == 'tools':
        outputs = OUTPUTS[name]
        if outputs and node.get('output') not in outputs:
            raise ValueError(f'{variable} 必须选择output: {outputs}')
        if not outputs and 'output' in node:
            raise ValueError(f'{variable} 不支持output')
    elif 'output' in node:
        raise ValueError('signal.* 不支持output')
    if 'timeframe' in node and node['timeframe'] not in TIMEFRAMES:
        raise ValueError(f'不支持的timeframe: {node["timeframe"]}')


def period_ends(sessions, timeframe):
    """已知交易时段对应的周期结束时间，包含未来边界以免逐Tick查日历。"""
    if timeframe.endswith('h'):
        return [end.to_pydatetime() for _, end in hour_windows(sessions, TIMEFRAMES[timeframe] // 60)]
    ends = {}
    for _, closing in sessions:
        key = closing.date()
        if timeframe == '1w':
            key -= timedelta(days=key.weekday())
        ends[key] = closing
    return list(ends.values())


def native_bars(frame, market, timeframe, now, config):
    """Provider daily/weekly bars: exclude the current unfinished period."""
    tz = market_of(market).timezone
    now = pd.Timestamp(now).tz_convert(tz)
    df = frame.copy().sort_index()
    index = pd.DatetimeIndex(df.index)
    index = index.tz_localize(tz) if index.tz is None else index.tz_convert(tz)
    periods = []
    for stamp in index:
        if timeframe == '1w':
            monday = stamp.normalize() - pd.Timedelta(days=stamp.weekday())
            periods.append([(monday + pd.Timedelta(days=i)).date() for i in range(5)])
        else:
            periods.append([stamp.date()])
    sessions = trading_sessions(market, {day for period in periods for day in period}, config=config)
    starts, ends = [], []
    for period in periods:
        windows = [(pd.Timestamp(start), pd.Timestamp(end)) for start, end in sessions if start.date() in period]
        starts.append(windows[0][0] if windows else pd.NaT)
        ends.append(windows[-1][1] if windows else pd.NaT)
    df['bar_start'], df['bar_end'] = starts, ends
    df.index = index
    df = df[df['bar_start'].notna() & (df['bar_start'] <= now)]
    return df[df['bar_end'] <= now]


def calculate(node, code, config, now, signal_func=None):
    namespace, name = node['variable'].split('.', 1)
    timeframe = timeframe_of(node, config)
    market = 'CN' if code.split('.')[0] in ('SH', 'SZ') else code.split('.')[0]
    params = {**(TOOL_PARAMS[name] if namespace == 'tools' else SIGNAL_PARAMS[name]),
              **node.get('params', {})}
    if namespace == 'signal' and name in SIGNAL_TYPES:
        from trends import _get_cached_stock_params
        kind = SIGNAL_TYPES[name]
        record = _get_cached_stock_params(config.get('CONFIG', f'{kind}_PARAMS_DB', fallback=None), code)
        if record:
            params = {**SIGNAL_PARAMS[name], **record['best_params'], **node.get('params', {})}
    validate_indicator({**node, 'params': params})
    params = {key: int(value) if key in PERIOD_PARAMS else value for key, value in params.items()}
    periods = [int(v) for k, v in params.items() if k in PERIOD_PARAMS]
    required = max(120, 4 * max(periods or [1])) if name not in ('MA',) else params['N']
    local_config = deepcopy(config)
    if timeframe.endswith('h'):
        # Minute bars guarantee open anchors; source limitations fail closed.
        local_config.set('CONFIG', 'FUTU_PUSH_TYPE', 'K_1M')
        minutes_per_bar = min(TIMEFRAMES[timeframe], 390 if market == 'US' else 180 if market == 'HK' else 120)
        count = (required + 2) * minutes_per_bar + 390
    else:
        local_config.set('CONFIG', 'FUTU_PUSH_TYPE', 'K_DAY' if timeframe == '1d' else 'K_WEEK')
        count = required + 2
    frame = get_kline_data(code, local_config, max_count=count, force_refresh=True)
    if frame is None or frame.empty:
        raise ValueError(f'{code} K线数据不可用')
    bars = (convert_to_Nhour(frame, n_hours=TIMEFRAMES[timeframe] // 60, market=market,
                             config=config, bar='closed', now=now) if timeframe.endswith('h')
            else native_bars(frame, market, timeframe, now, config))
    if len(bars) < required:
        raise ValueError(f'{code} {timeframe} K线不足，需要{required}根，实际{len(bars)}根')
    close = bars['close']
    if namespace == 'signal':
        df = bars[['open', 'high', 'low', 'close', 'volume']].copy()
        df.index = df.index.tz_localize(None)
        if name == 'balance':
            value = signal_func(df, **params) or ''
        else:
            value = signal_func(df, code, config, params=params) or ''
    elif name in ('MA', 'EMA', 'RSI'):
        value = getattr(tools, name)(close, **params)[-1]
    elif name == 'KDJ':
        outputs = tools.KDJ(close, bars['high'], bars['low'], **params)
        value = outputs[OUTPUTS[name].index(node['output'])][-1]
    else:
        dif, dea = tools.MACD(close, **params)
        outputs = {'DIF': dif, 'DEA': dea, 'HIST': 2 * (dif - dea)}
        value = outputs[node['output']].iloc[-1]
    if namespace == 'tools':
        value = float(value)
        if not math.isfinite(value):
            raise ValueError(f'{code} {node["variable"]} 结果为NaN/Inf，跳过评估')
    return {'value': value, 'params': params, 'timeframe': timeframe,
            'bar_start': bars.iloc[-1]['bar_start'].isoformat(),
            'bar_end': bars.iloc[-1]['bar_end'].isoformat()}
