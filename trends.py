import json
import os
from ft_config import get_config
from data import get_kline
from signal_analysis import detect_stochastic_signals_vectorized
from tools import *
import datetime
import configparser
import numpy as np
from telegram_engine import TelegramBotEngine
from email_engine import EmailEngine
from decimal import Decimal, ROUND_HALF_UP

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

def is_reverse(code:str, df:pd.DataFrame|None, config:configparser.ConfigParser)->str|None:
    """
    检查是否出现反转信号
    
    Args:
        code: 股票代码
        df: 股票数据
        config: 配置对象
    
    Returns:
        str|None: 返回反转信号描述或None
    """
    assert len(df) >= 90
    
    # 从配置中获取参数文件路径
    params_file = config.get("CONFIG", "KD_PARAMS", fallback=None)
    if not params_file or not os.path.exists(params_file):
        raise(f"Warning: KD parameters file not found at {params_file}")
    
    # 读取JSON文件
    with open(params_file, 'r') as f:
        all_params = json.load(f)
        
    # 获取特定代码的参数
    if code not in all_params:
        print(f"Warning: No parameters found for {code}")
        return 'No parameters'
        
    code_params = all_params[code]
    
    # 提取参数
    params = code_params['best_params']
    
    # 使用参数进行信号检测
    result = detect_stochastic_signals_vectorized(
        df,
        k_period=params['k_period'],
        d_period=params['d_period'],
        overbought=params['overbought'],
        oversold=params['oversold'],
        support_ma_period=params['support_ma_period'],
        resistance_ma_period=params['resistance_ma_period'],
        atr_period_explicit=params['atr_period_explicit'],
        atr_period_hidden=params['atr_period_hidden'],
        strength_threshold=params['strength_threshold']
    )
    
    # 获取最后一行的信号
    last_row = result.iloc[-1]
    # 处理 last_row['reversal'] 的两种情况
    reversal_value = last_row['reversal']
    strong_value = last_row['is_strong']
        
    # 确保 reversal_value 是字符串
    if isinstance(reversal_value, pd.Series):
        reversal_value = reversal_value.item()  # 提取单个值
    if isinstance(strong_value, pd.Series):
        strong_value = strong_value.item()  # 提取单个值
    
    # 检查是否有反转信号
    msg = ''
    if reversal_value != 'none':
        msg += reversal_value
    if strong_value == 1:
        msg += u'🚨'
    return None if msg == '' else msg

def is_continue(data:pd.DataFrame)->str|None:# 检查macd趋势延续/背离
    assert len(data) >= 40
    # 计算MACD
    dif, dea = MACD(data['close'], 12, 26, 9)
    data['DIF'] = dif
    data['DEA'] = dea
    data['Signal_Output'] = 0  # 趋势信号: 1上升, -1下降, 0无信号
    
    # 获取交叉状态
    crossover = crossover_status(data['DIF'], data['DEA'])
    golden_crosses = [i for i, c in enumerate(crossover) if c == 1]  # 金叉索引
    dead_crosses = [i for i, c in enumerate(crossover) if c == -1]  # 死叉索引
    idxs = data.index.tolist()

    # 趋势延续信号
    for gc in golden_crosses:
        prev_dc = next((dc for dc in reversed(dead_crosses) if dc < gc), None)
        if prev_dc and data['DEA'].iloc[prev_dc:gc+1].min() > 0:
            data.loc[idxs[gc], 'Signal_Output'] = 1  # 上升趋势延续

    for dc in dead_crosses:
        prev_gc = next((gc for gc in reversed(golden_crosses) if gc < dc), None)
        if prev_gc and data['DEA'].iloc[prev_gc:dc+1].max() < 0:
            data.loc[idxs[dc], 'Signal_Output'] = -1  # 下降趋势延续
    
    # 背离检测
    divergence = detect_divergence(data['DIF'], data['DEA'], data['close'], golden_crosses, dead_crosses)
    
    # 检查最后一行
    last_row_idx = data.index[-1]
    continuation = data.loc[last_row_idx, 'Signal_Output']
    div_value = divergence.iloc[-1]
    
    msg = ''
    if continuation == 1:
        msg += '上升趋势延续'
    if continuation == -1:
        msg += '下降趋势延续'
    if div_value == 1:
        msg += '顶背离🚨'
    if div_value == -1:
        msg += '底背离🚨'
    
    return None if msg == '' else msg

def is_breakout(high, low, close, N:int=10)->str|None:# 最近一根K线突破/跌破均线
    close_ema = EMA(close, N)
    last_close = round_decimal(close.iloc[-1])
    last_ema = round_decimal(close_ema[-1])
    prev_close = round_decimal(close.iloc[-2])
    prev_ema = round_decimal(close_ema[-2])
    if last_close > last_ema and prev_close <= prev_ema:
        return f'突破ema{N}'
    if last_close < last_ema and prev_close >= prev_ema:
        return f'跌破ema{N}'
    return None

def is_top_down(data:pd.DataFrame) -> str|None:# 判别 KDJ 指标的顶部和底部信号
    assert len(data) >= 40
    # 计算KDJ
    k,d,j = KDJ(data['close'], data['high'], data['low'])
    data['K'] = k
    data['D'] = d
    data['J'] = j
    
    # 获取K和D的交叉状态
    crossover = crossover_status(data['K'], data['D'])
    golden_crosses = [i for i, c in enumerate(crossover) if c == 1]  # 金叉索引
    dead_crosses = [i for i, c in enumerate(crossover) if c == -1]  # 死叉索引
    
    j_values = data['J']
    d_values = data['D']
    msg = str(round_decimal(d_values.iloc[-1],1))

    if j_values.iloc[-1] <= 100 and all(j > 100 for j in j_values[-4:-2]):
        msg += f'顶消失🚨'
    # 底消失信号
    elif j_values.iloc[-1] >= 0 and all(j < 0 for j in j_values[-4:-2]):
        msg += f'底消失🚨'
    
    if d_values.iloc[-1] > d_values.iloc[-2]:
        msg += '↑'
    else:
        msg += '↓'
    
    # 背离检测
    divergence = detect_divergence(data['K'], data['D'], data['close'], golden_crosses, dead_crosses)
    div_value = divergence.iloc[-1]
    
    if div_value == 1:
        msg += '顶背离🚨'
    if div_value == -1:
        msg += '底背离🚨'

    return msg

def get_rank(high, low, close) -> float:  # 动量因子 r_squared：基于年化收益和判定系数打分
    # 计算动量因子
    return calc_momentum(close)

def check_trends(code_in_group, config: configparser.ConfigParser):
    trends = []
    trend_type = config.get("CONFIG", "TREND_TYPE").split(',')
    if code_in_group.size and len(trend_type):
        name_list = code_in_group['name']
        trends_with_rank = []
        for idx, futu_code in enumerate(code_in_group['code'].values):
            df = get_kline(futu_code, config)  # 获取 DataFrame

            # 添加对 df 的检查
            if df is None:
                print(f"Warning: Failed to get data for {futu_code}")
                continue
                
            if len(df) == 0:
                print(f"Warning: Empty data for {futu_code}")
                continue

            high = pd.Series(df['high'].values.ravel())  # 从 DataFrame 中提取 high 列并转换为 pd.Series
            low = pd.Series(df['low'].values.ravel())    # 从 DataFrame 中提取 low 列并转换为 pd.Series
            close = pd.Series(df['close'].values.ravel())  # 从 DataFrame 中提取 close 列并转换为 pd.Series
            name = name_list[idx]

            if len(high) == 0 or len(low) == 0 or len(close) == 0:
                print(f"Warning: No data for {futu_code}")
                continue

            msg = '{} {} '.format(futu_code, name)
            for i in trend_type:
                if i.lower() == 'breakout':
                    bo = is_breakout(high,low,close) # 突破/跌破EMA均线
                    if bo is not None:
                        msg += bo
                elif i.lower() == 'reverse':
                    rev = is_reverse(futu_code,df,config) # 趋势反转
                    if rev is not None:
                        msg += rev
                elif i.lower() == 'continue':
                    co = is_continue(df) # 趋势延续
                    if co is not None:
                        msg += co
                elif i.lower() == 'topdown':
                    td = is_top_down(df) # 顶底结构
                    if td is not None:
                        msg += td
            # 计算每个股票的动量因子并排序
            trends_with_rank.append([msg, get_rank(high, low, close)])         
        trends_with_rank.sort(key=lambda x: x[1], reverse=True)  # 根据动量因子排序
        trends = [trend[0] for trend in trends_with_rank]  # 返回排序后的趋势列表

    return trends

if __name__ == "__main__":
    config = get_config()
    host = config.get("CONFIG", "FUTU_HOST")
    port = int(config.get("CONFIG", "FUTU_PORT"))
    group = config.get("CONFIG", "FUTU_GROUP")
    telegram = config.get("CONFIG", "TELEGRAM_BOT_TOKEN")
    emails = config.get("CONFIG", "EMAIL_SUBSCRIBTION").split(',')

    ls = code_in_futu_group(group,host,port)
    trends = check_trends(ls,config)
    
    if telegram:
        telebot = TelegramBotEngine(config)
        telebot.send_telegram_message('{} {}:\n{}'.format(datetime.datetime.now().strftime('%Y-%m-%d'), group, '\n'.join(trends)),'https://www.futunn.com/')

    if emails[0]:
        emailWorker = EmailEngine(config)
        for address in emails:
            emailWorker.send_email(address,group,'<p>{} {}:<br>{}</p>'.format(datetime.datetime.now().strftime('%Y-%m-%d'), group, '<br>'.join(trends)))