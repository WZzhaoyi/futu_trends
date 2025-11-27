import json
import os
from ft_config import get_config
from data import get_kline_data
from params_db import ParamsDB
from signal_analysis import get_target_price, MACD, KD, RSI
from tools import MA, EMA, calc_momentum, code_in_futu_group
import datetime
import configparser
from notification_engine import NotificationEngine
from decimal import Decimal, ROUND_HALF_UP
import pandas as pd

def round_decimal(value, places=2):
    """
    将浮点数转换为 Decimal 并四舍五入到指定小数位数
    """
    if isinstance(value, float):
        value = str(value)  # 将 float 转换为字符串以避免精度损失
    return Decimal(value).quantize(Decimal(f'0.{"0" * places}'), rounding=ROUND_HALF_UP)

def inside_MA(close, last_low, last_high): # 计算MA5,10,15中的最小值和最大值 对比是否区间重叠
    last_index = close.size - 1
    ma5 = round_decimal(MA(close,5)[last_index])
    ma10 = round_decimal(MA(close,10)[last_index])
    ma15 = round_decimal(MA(close,15)[last_index])

    min_ma = min(ma5, ma10, ma15)
    max_ma = max(ma5, ma10, ma15)

    if max_ma < last_low or min_ma > last_high: # 如果第一个区间的结束小于第二个区间的开始，或者第一个区间的开始大于第二个区间的结束，说明不重叠
        return False
    else:
        return True

def is_reverse(df: pd.DataFrame, code: str, config: configparser.ConfigParser) -> str | None:
    """检查是否出现反转信号"""
    assert len(df) >= 90
    
    # 从数据库读取参数
    db_path = config.get("CONFIG", "KD_PARAMS_DB", fallback=None)
    if db_path is None:
        return None
    db = ParamsDB(db_path)
    data = db.get_stock_params(code)
    if data is None:
        return 'No KD parameters'
    params = data['best_params']
    meta = data['meta_info']
    performance = data['performance']
            
    if not params:
        print(f"No KD parameters found for {code}")
        return 'No KD parameters'
    
    # 信号检测
    result = KD().calculate(df, params)
    
    # 获取最后一行的信号
    last_row = result.iloc[-1]
    reversal =  last_row['reversal']
    is_strong =  last_row['is_strong']
    
    # 检查是否有反转信号
    msg = ''
    if reversal != 'none' and type(reversal) == str:
        msg += reversal.replace('reversal','kd')
        target_low, target_high = get_target_price(df, target_multiplier=meta['target_multiplier'], atr_period=meta['atr_period'])
        msg += f' [{target_low},{target_high}]'
    if 'support' in reversal:
        msg += u'📈'
    elif 'resistance' in reversal:
        msg += u'📉'
    return None if msg == '' else msg

def is_continue(df:pd.DataFrame, code:str, config:configparser.ConfigParser)->str|None:# 检查macd趋势延续
    assert len(df) >= 90
    
    # 从数据库读取参数
    db_path = config.get("CONFIG", "MACD_PARAMS_DB", fallback=None)
    if db_path is None:
        return None
    db = ParamsDB(db_path)
    data = db.get_stock_params(code)
    if data is None:
        return 'No MACD parameters'
    params = data['best_params']
    meta = data['meta_info']
    performance = data['performance']
            
    if not params:
        print(f"No MACD parameters found for {code}")
        return 'No MACD parameters'
            
    # 信号检测
    result = MACD().calculate(df, params)
    
    # 获取最后一行的信号
    last_row = result.iloc[-1]
    reversal =  last_row['reversal']
    is_strong =  last_row['is_strong']
    
    # 检查是否有反转信号
    msg = ''
    if reversal != 'none' and type(reversal) == str:
        msg += reversal.replace('reversal','macd')
        target_low, target_high = get_target_price(df, target_multiplier=meta['target_multiplier'], atr_period=meta['atr_period'])
        msg += f' [{target_low},{target_high}]'
    if 'support' in reversal:
        msg += u'📈'
    elif 'resistance' in reversal:
        msg += u'📉'
    return None if msg == '' else msg

def is_breakout(data:pd.DataFrame, code:str, config:configparser.ConfigParser)->str|None:# K线突破/跌破均线
    assert len(data) >= 90
    N = 240
    # 从数据库读取参数 默认N=240
    db_path = config.get("CONFIG", "EMA_PARAMS_DB", fallback=None)
    if db_path is not None:
        db = ParamsDB(db_path)
        data = db.get_stock_params(code)
        if data is not None:
            N = data['best_params']['ema_period']

    close = data['close']
    close_ema = EMA(close, N)
    last_close = round_decimal(close.iloc[-1])
    last_ema = round_decimal(close_ema[-1])
    prev_close = round_decimal(close.iloc[-2])
    prev_ema = round_decimal(close_ema[-2])
    prev_prev_ema = round_decimal(close_ema[-3])
    msg = ''
    if last_close > last_ema and prev_close <= prev_ema:
        msg = f'breakthrough ema{N}📈'
    elif last_close < last_ema and prev_close >= prev_ema:
        msg = f'breakdown ema{N}📉'
    elif prev_prev_ema < prev_ema and prev_ema > last_ema:
        msg = f'decline ema{N}📉'
    elif prev_prev_ema > prev_ema and prev_ema < last_ema:
        msg = f'rise ema{N}📈'
    return None if msg == '' else msg

def is_top_down(df:pd.DataFrame, code:str, config:configparser.ConfigParser) -> str|None:# 顶部和底部
    assert len(df) >= 90
    # 从数据库读取参数
    db_path = config.get("CONFIG", "RSI_PARAMS_DB", fallback=None)
    if db_path is None:
        return None
    db = ParamsDB(db_path)
    data = db.get_stock_params(code)
    if data is None:
        return 'No RSI parameters'
    params = data['best_params']
    meta = data['meta_info']
    performance = data['performance']
            
    if not params:
        print(f"No RSI parameters found for {code}")
        return 'No RSI parameters'
            
    # 信号检测
    result = RSI().calculate(df, params)
    last_row = result.iloc[-1]
    reversal =  last_row['reversal']
    is_strong =  last_row['is_strong']

    msg = ''
    
    # 检查是否有反转信号
    if reversal != 'none' and type(reversal) == str:
        msg += reversal.replace('reversal','rsi')
        target_low, target_high = get_target_price(df, target_multiplier=meta['target_multiplier'], atr_period=meta['atr_period'])
        msg += f' [{target_low},{target_high}]'
    if 'support' in reversal:
        msg += u'📈'
    elif 'resistance' in reversal:
        msg += u'📉'
    
    return None if msg == '' else msg

def is_balance(data: pd.DataFrame, M: int = 5, N: int = 28, R: int = 20) -> str | None: # 量价关系平衡
    assert len(data) >= 90

    sum_cvol = (data['close'] * data['volume']).rolling(N, min_periods=N).sum()
    sum_vol = data['volume'].rolling(N, min_periods=N).sum()
    ma_c = data['close'].rolling(N, min_periods=N).mean()
    vpc = sum_cvol / sum_vol - ma_c

    sum_cvol = (data['close'] * data['volume']).rolling(M, min_periods=M).sum()
    sum_vol = data['volume'].rolling(M, min_periods=M).sum()
    ma_c = data['close'].rolling(M, min_periods=M).mean()
    vpr = (sum_cvol / sum_vol) / ma_c

    vm = data['volume'].rolling(M, min_periods=M).mean() / data['volume'].rolling(N, min_periods=N).mean()

    vpci = vpc*vpr*vm

    dis = vpci.rolling(R, min_periods=R).std()
    mid = vpci.rolling(R, min_periods=R).mean()
    upper = mid + 2 * dis
    lower = mid - 2 * dis

    msg = ''

    if vpci.iloc[-1] > lower.iloc[-1] and vpci.iloc[-2] < lower.iloc[-2] and vpci.iloc[-3] < lower.iloc[-3]:
        msg += 'support vpci📈'
    elif vpci.iloc[-1] < upper.iloc[-1] and vpci.iloc[-2] > upper.iloc[-2] and vpci.iloc[-3] > upper.iloc[-3]:
        msg += 'resistance vpci📉'

    return None if msg == '' else msg

def check_trends(code_in_group: pd.DataFrame, config: configparser.ConfigParser):
    """
    检查股票趋势并返回DataFrame格式的结果
    返回的DataFrame包含以下列：
    - name: 股票名称
    - msg: 趋势信息
    - momentum: 动量因子值
    """
    trend_type = config.get("CONFIG", "TREND_TYPE").split(',')
    momentum_period = int(config.get("CONFIG", "MOMENTUM_PERIOD", fallback=21))
    if not (code_in_group.size and len(trend_type)):
        return pd.DataFrame(columns=pd.Index(['futu_code', 'name', 'msg', 'momentum', 'high', 'low']))
        
    results = []
    for idx, futu_code in enumerate(code_in_group['code'].values):
        print(f"Processing {futu_code}")
        try:
            df = get_kline_data(futu_code, config, max_count=1000)

            # 添加对 df 的检查
            if df is None:
                print(f"Warning: Failed to get data for {futu_code}")
                continue
                
            if len(df) == 0:
                print(f"Warning: Empty data for {futu_code}")
                continue

            name = code_in_group['name'].iloc[idx]

            if len(df['high']) == 0 or len(df['low']) == 0 or len(df['close']) == 0:
                print(f"Warning: No data for {futu_code}")
                continue

            msg = f'{futu_code} {name}'
            for i in trend_type:
                if i.lower() == 'breakout':
                    bo = is_breakout(df,futu_code,config) # 突破/跌破EMA均线
                    if bo is not None:
                        msg += f' | {bo}'
                elif i.lower() == 'reverse':
                    rev = is_reverse(df,futu_code,config) # 趋势反转
                    if rev is not None:
                        msg += f' | {rev}'
                elif i.lower() == 'continue':
                    co = is_continue(df,futu_code,config) # 趋势延续
                    if co is not None:
                        msg += f' | {co}'
                elif i.lower() == 'topdown':
                    td = is_top_down(df,futu_code,config) # 顶底结构
                    if td is not None:
                        msg += f' | {td}'
                elif i.lower() == 'balance':
                    bal = is_balance(df) # 量价关系平衡
                    if bal is not None:
                        msg += f' | {bal}'
            
            # 计算动量因子
            close = df['close']
            momentum = calc_momentum(close, momentum_period) if isinstance(close, pd.Series) else pd.Series([0.000,0.000])
            
            # 获取最后两个动量值，用于判断方向
            last_momentum = momentum.iloc[-1]
            prev_momentum = momentum.iloc[-2]
            msg += f' | {last_momentum:.3f}'

            if last_momentum > prev_momentum:
                msg += u'↑'
            elif last_momentum < prev_momentum:
                msg += u'↓'
            
            # 添加到结果列表

            recent_high = df['high'].iloc[-3:].max()
            recent_low = df['low'].iloc[-3:].min()

            results.append({
                'futu_code': futu_code,
                'name': name,
                'msg': msg,
                'momentum': last_momentum,
                'high': recent_high,
                'low': recent_low
            })
        except Exception as e:
            print(f"Error processing {futu_code}: {str(e)}")
            continue
    
    # 创建DataFrame并按动量因子排序
    if results:
        # 添加一行动量值为0的记录作为0轴指示
        results.append({
            'futu_code': 'ZERO_AXIS',
            'name': f'{momentum_period}动量0轴',
            'msg': f'━━━{momentum_period}动量0轴━━━',
            'momentum': 0.000,
            'high': 0.000,
            'low': 0.000
        })
        
        result_df = pd.DataFrame(results)
        result_df.set_index('futu_code', inplace=True)
        result_df.sort_values('momentum', ascending=False, inplace=True)
        return result_df
    else:
        return pd.DataFrame(columns=pd.Index(['futu_code', 'name', 'msg', 'momentum', 'high', 'low']))

if __name__ == "__main__":
    config = get_config()
    host = config.get("CONFIG", "FUTU_HOST")
    port = int(config.get("CONFIG", "FUTU_PORT"))
    group = config.get("CONFIG", "FUTU_GROUP", fallback='')
    code_list = config.get("CONFIG", "FUTU_CODE_LIST", fallback='').split(',')
    code_list = [code for code in code_list if code.strip()]
    push_type = config.get("CONFIG", "FUTU_PUSH_TYPE")

    # 获取股票列表
    code_pd = pd.DataFrame(columns=pd.Index(['code','name']))
    if group:
        ls = code_in_futu_group(group,host,port)
        if isinstance(ls, pd.DataFrame):
            code_pd = pd.concat([code_pd, ls[['code','name']]])
    if len(code_list) > 0:
        ls = pd.DataFrame({'code': code_list, 'name': code_list})
        code_pd = pd.concat([code_pd, ls])

    if code_pd.empty:
        print('warning: no code in config')
        exit()

    assert isinstance(code_pd, pd.DataFrame), "code_pd must be a DataFrame"
    trends_df = check_trends(code_pd,config)
    if trends_df.empty:
        print('warning: no trends data')
        exit()
    raw_msg = '{} {} {}:\n{}'.format(datetime.datetime.now().strftime('%Y-%m-%d'), group if group else '', push_type, '\n'.join(trends_df['msg']))
    filter_df = trends_df[trends_df['msg'].str.count('\\|') >= 2]

    notification = NotificationEngine(config)

    # futu分组/到价提醒
    if len(filter_df) > 0:
        target_prices = filter_df['msg'].str.extract(r'\[(\d+\.\d+),(\d+\.\d+)\]')
        notification.send_futu_message([str(code) for code in filter_df.index.tolist()],filter_df['msg'].tolist(),target_prices[1].tolist(),target_prices[0].tolist())

    # 原始消息
    notification.send_telegram_message(raw_msg,'https://www.futunn.com/')
    notification.send_email(f'{group} {push_type}',raw_msg)

    # google sheet
    notification.send_google_sheet_message('{} {} {}:\n{}'.format(datetime.datetime.now().strftime('%Y-%m-%d'), group if group else '', push_type, '\n'.join(filter_df['msg'])))
