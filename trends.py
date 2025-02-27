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

def is_continue(high, low)->str|None:# 取倒数5根AO柱 近3根趋势与之前相反
    ao = AO(high, low)
    if len(ao) < 5:
        return None
    
    # 使用 round_decimal 处理 AO 中的每个元素
    last_five = [round_decimal(x) for x in ao[-5:]]
    
    [a1, a2, a3, a4, a5] = last_five  # 解包最后五个元素以便于比较

    if a3 < a4 < a5 and a1 > a2 < a3 and a3 < 0:
        return '上涨可能延续'
    if a3 > a4 > a5 and a1 < a2 > a3 and a3 > 0:
        return '下跌可能延续'
    
    return None

def is_breakout(high, low, close, N:int=5)->str|None:# 最近一根K线突破/跌破均线
    high_ema = EMA(high, N)
    low_ema = EMA(low, N)
    last_index = len(close) - 1
    prev_index = last_index - 1
    last_close = round_decimal(close.iloc[last_index])
    prev_close = round_decimal(close.iloc[prev_index])
    last_high_ema = round_decimal(high_ema[last_index])
    last_low_ema = round_decimal(low_ema[last_index])
    points = np.array([last_close,high.iloc[last_index],low.iloc[last_index]])
    if ((last_low_ema<=points) & (points<=last_high_ema)).any():
        return '触及均线'
    if last_close > last_high_ema and prev_close <= round_decimal(high_ema[prev_index]):
        return '突破均线'
    if last_close < last_low_ema and prev_close >= round_decimal(low_ema[prev_index]):
        return '跌破均线'
    return None

def is_top_down(high, low, close) -> str|None:# 判别 KDJ 指标的顶部和底部信号
    kdj_df = KDJ(close, high, low)

    assert len(kdj_df) >= 40

    j_values = kdj_df['J']  # 获取最后周期的 J 值
    d_values = kdj_df['D']  # 获取最后周期的 D 值
    last_j = round_decimal(j_values.iloc[-1],1)  # 获取最后一个 J 值
    msg = u'🚨'+str(last_j) if last_j>100 or last_j<0 else str(last_j)

    # # 顶部信号
    # if all(j > 100 for j in j_values[-2:]) and j_values.iloc[-4] <= 100:
    #     msg += f'顶部'
    # # 底部信号
    # elif all(j < 0 for j in j_values[-2:]) and j_values.iloc[-4] >= 0:
    #     msg += f'底部'
    # 顶消失信号
    if last_j <= 100 and all(j > 100 for j in j_values[-4:-2]):
        msg += f'顶消失'
    # 底消失信号
    elif last_j >= 0 and all(j < 0 for j in j_values[-4:-2]):
        msg += f'底消失'

    # 使用 crossover_status 判断 J 和 D 的交叉情况
    crossover_results = crossover_status(j_values, d_values)
    # 遍历 crossover_results 判断底背离和顶背离
    for i in range(1, len(crossover_results)-1):
        if crossover_results[-i] == 1:  # J 上穿 D
            for j in range(i + 1, len(crossover_results)):
                if crossover_results[-j] == 1:  # 上一次 J 上穿 D
                    bd = (d_values.iloc[-i] > d_values.iloc[-j]) and (low.iloc[-i] < low.iloc[-j])
                    if bd:
                        msg += '底背离'
                break
            break  # 找到后退出内层循环
        elif crossover_results[-i] == -1:  # D 上穿 J
            for j in range(i + 1, len(crossover_results)):
                if crossover_results[-j] == -1:  # 上一次 D 上穿 J
                    td = (d_values.iloc[-i] < d_values.iloc[-j]) and (high.iloc[-i] > high.iloc[-j])
                    if td:
                        msg += '顶背离'
                break
            break  # 找到后退出内层循环
    
    if kdj_df['J'].iloc[-1] > kdj_df['J'].iloc[-2]:
        msg += '↑'
    else:
        msg += '↓'
    
    return  msg

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
                    co = is_continue(high,low) # 趋势延续
                    if co is not None:
                        msg += co
                elif i.lower() == 'topdown':
                    td = is_top_down(high,low,close) # 顶底结构
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