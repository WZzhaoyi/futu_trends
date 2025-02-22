from ft_config import get_config
from data import get_kline
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

def is_reverse(high, low, close)->str|None:# 最近一根K线创新高/低 收盘价位于K线下半部/上半部
    if len(close) == 0:
        return None
    
    last_index = len(close) - 1
    if last_index < 3:  # 确保有足够的数据进行比较
        return None
    
    last_close = close[last_index]
    last_low = low[last_index]
    last_high = high[last_index]
    last_ave = (last_high+last_low)/2
    if inside_MA(close, last_low, last_high):
        return None
    if last_low < low[last_index-1] and last_low < low[last_index-2] and last_ave < last_close and last_low < low[last_index-3]:
         return '下跌可能反转'
    if last_high > high[last_index-1] and last_high > high[last_index-2] and last_ave > last_close and last_high > high[last_index-3]:
         return '上涨可能反转'
    return None

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

def is_breakout(high, low, close, N:int=55)->str|None:# 最近一根K线突破/跌破均线
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
            high = df['high']  # 从 DataFrame 中提取 high 列
            low = df['low']    # 从 DataFrame 中提取 low 列
            close = df['close']  # 从 DataFrame 中提取 close 列
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
                    rev = is_reverse(high,low,close) # 趋势反转
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