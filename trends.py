from tools import *
import datetime
import configparser
import os
from telegram_engine import TelegramBotEngine
from email_engine import EmailEngine

def insideMA(close, last_low, last_high): # 计算MA5,10,15中的最小值和最大值 对比是否区间重叠
    last_index = close.size - 1
    ma5 = MA(close,5)[last_index]
    ma10 = MA(close,10)[last_index]
    ma15 = MA(close,15)[last_index]

    min_ma = min(ma5, ma10, ma15)
    max_ma = max(ma5, ma10, ma15)

    if max_ma < last_low or min_ma > last_high: # 如果第一个区间的结束小于第二个区间的开始，或者第一个区间的开始大于第二个区间的结束，说明不重叠
        return False
    else:
        return True

def isReverse(high, low, close)->str|None:# 最近一根K线创新高/低 收盘价位于K线下半部/上半部
    if len(close) == 0:
        return None
    
    last_index = len(close) - 1
    if last_index < 3:  # 确保有足够的数据进行比较
        return None
    
    last_close = close[last_index]
    last_low = low[last_index]
    last_high = high[last_index]
    last_ave = (last_high+last_low)/2
    if insideMA(close, last_low, last_high):
        return None
    if last_low < low[last_index-1] and last_low < low[last_index-2] and last_ave < last_close and last_low < low[last_index-3]:
         return '下跌可能反转'
    if last_high > high[last_index-1] and last_high > high[last_index-2] and last_ave > last_close and last_high > high[last_index-3]:
         return '上涨可能反转'
    return None

def isContinue(high, low)->str|None:# 取倒数5根AO柱 近3根趋势与之前相反
    ao = AO(high, low)
    [a1, a2, a3, a4, a5] = ao[-5:]
    if a3 < 0 and a1 > a2 and  a2 < a3 and a3 < a4 and a4 < a5:
         return '上涨可能延续'
    if a3 > 0 and a1 < a2 and a2 > a3 and a3 > a4 and a4 > a5:
         return '下跌可能延续'
    return None

def isBreakout(high, low, close)->str|None:# 最近一根K线突破/跌破EMA60日均线
    ma60 = MA(close, 60)
    last_index = close.size - 1
    last_close = close[last_index]
    prev_close = close[last_index-1]
    last_ma60 = ma60[last_index]
    if last_close > ma60[last_index] and prev_close <= last_ma60:
        return '突破60日均线'
    if last_close < ma60[last_index] and prev_close >= last_ma60:
        return '跌破60日均线'
    return None

def checkTrends(code_in_group, config: configparser.ConfigParser):
     
    trends = []
    type = config.get("CONFIG", "FUTU_PUSH_TYPE")
    host = config.get("CONFIG", "FUTU_HOST")
    port = int(config.get("CONFIG", "FUTU_PORT"))
    trend_type = config.get("CONFIG", "TREND_TYPE").split(',')
    if code_in_group.size and len(trend_type):
         name_list = code_in_group['name']
         for idx, futu_code in enumerate(code_in_group['code'].values):
            high, low, close = kline(futu_code, ktype=type, host=host, port=port)

            if len(high) == 0 or len(low) == 0 or len(close) == 0:
                print(f"Warning: No data for {futu_code}")
                continue

            for i in trend_type:
                if i.lower() == 'breakout':
                    bo = isBreakout(high,low,close) # 突破/跌破EMA60日均线
                    if bo is not None:
                        trends.append('{} {} {}'.format(futu_code, name, bo))
                elif i.lower() == 'reverse':
                    rev = isReverse(high,low,close) # 趋势反转
                    if rev is not None:
                        trends.append('{} {} {}'.format(futu_code, name, rev))
                elif i.lower() == 'continue':
                    co = isContinue(high,low) # 趋势延续
                    if co is not None:
                        trends.append('{} {} {}'.format(futu_code, name, co))
            name = name_list[idx]
    return trends

if __name__ == "__main__":
    BASE_DIR = os.path.split(os.path.realpath(__file__))[0]
    config = configparser.ConfigParser()
    config.read(os.path.join(BASE_DIR, 'config.ini'), encoding='utf-8')
    host = config.get("CONFIG", "FUTU_HOST")
    port = int(config.get("CONFIG", "FUTU_PORT"))
    group = config.get("CONFIG", "FUTU_GROUP")
    telegram = config.get("CONFIG", "TELEGRAM_BOT_TOKEN")
    emails = config.get("CONFIG", "EMAIL_SUBSCRIBTION").split(',')

    ls = codeInFutuGroup(group,host,port)
    trends = checkTrends(ls,config)
    
    if telegram is not None:
        telebot = TelegramBotEngine(config)
        telebot.send_telegram_message('{} {}:\n{}'.format(datetime.datetime.now().strftime('%Y-%m-%d'), group, '\n'.join(trends)),'https://www.futunn.com/')

    if emails is not None and len(emails):
        emailWorker = EmailEngine()
        for address in emails:
            emailWorker.send_email(address,group,'<p>{} {}:<br>{}</p>'.format(datetime.datetime.now().strftime('%Y-%m-%d'), group, '<br>'.join(trends)))