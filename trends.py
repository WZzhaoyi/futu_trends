from tools import *
import datetime
import configparser
import os
from telegram_bot import TelegramBotEngine
from email_engine import EmailEngine

def isReverse(high, low, close)->str|None:# 最近一根K线创新高/低 收盘价位于K线下半部/上半部
    last_index = close.size - 1
    last_close = close[last_index]
    last_low = low[last_index]
    last_high = high[last_index]
    last_ave = (last_high+last_low)/2
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

def checkTrends(code_in_group):
     
    trends = []
    if code_in_group.size:
         print('----{} check trends for {}----'.format(datetime.datetime.now(), group))
         name_list = code_in_group['name']
         for idx, futu_code in enumerate(code_in_group['code'].values):
            high, low, close = kline(futu_code)
            print('download {} data'.format(futu_code))
            rev = isReverse(high,low,close) # 趋势反转
            co = isContinue(high,low) # 趋势延续
            name = name_list[idx]
            if rev is not None:
                trends.append('{} {} {}'.format(futu_code, name, rev))
            if co is not None:
                trends.append('{} {} {}'.format(futu_code, name, co))
    if len(trends) == 0:
        trends.append('未满足趋势特征')
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

    if emails is not None and len(emails):
        emailWorker = EmailEngine()
        for address in emails:
            emailWorker.send_email(address,group,'<p>{} {}:<br>{}</p>'.format(datetime.datetime.now().strftime('%Y-%m-%d'), group, '<br>'.join(trends)))