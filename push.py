import configparser
import datetime
import os
import time
import futu as ft
import pandas as pd
from telegram_engine import TelegramBotEngine

from tools import codeInFutuGroup, get_kline_seconds
from trends import checkTrends, isBreakout
class CurKlineCallback(ft.CurKlineHandlerBase):
    def __init__(self, callback=None, seconds=15 * 60):
        super(CurKlineCallback,self).__init__()
        self.callback = callback
        self.last_check_time = datetime.datetime.now()
        self.seconds = seconds

    def on_recv_rsp(self, rsp_pb):
        ret_code, data = super(CurKlineCallback,self).on_recv_rsp(rsp_pb)
        current_time = datetime.datetime.now()
        if (current_time - self.last_check_time).total_seconds() >= self.seconds:  # 每15分钟检查一次
            self.last_check_time = current_time
            with pd.option_context('display.max_columns', None, 'display.width', None, 'display.max_rows', None, 'display.max_colwidth', None):
                if ret_code != ft.RET_OK:
                    print('{} Kline:\n error, msg:{}'.format(datetime.datetime.now().strftime('%Y-%m-%d-%H:%M:%S'), data))
                    return ft.RET_ERROR, data
                print('{} Kline:\n {}'.format(datetime.datetime.now().strftime('%Y-%m-%d-%H:%M:%S'), data)) # CurKline 处理逻辑
            if self.callback is not None:
                self.callback(data)
        return ft.RET_OK, data

def create_callback(config: configparser.ConfigParser):
    def send_message(data):
        telebot = TelegramBotEngine(config)
        trends = checkTrends(data, config)
        
        if len(trends) >= 1:
            current_time = datetime.datetime.now()
            message = '{}:\n{} {}'.format(current_time.strftime('%Y-%m-%d %H:%M:%S'), data['name'][0], '\n'.join(trends))
            telebot.send_telegram_message(message, 'https://www.futunn.com/')
        return ft.RET_OK, data
    return send_message


if __name__ == '__main__':
    BASE_DIR = os.path.split(os.path.realpath(__file__))[0]
    config = configparser.ConfigParser()
    config.read(os.path.join(BASE_DIR, 'config.ini'), encoding='utf-8')
    host = config.get("CONFIG", "FUTU_HOST")
    port = int(config.get("CONFIG", "FUTU_PORT"))
    group = config.get("CONFIG", "FUTU_PUSH_GROUP")
    type = config.get("CONFIG", "FUTU_PUSH_TYPE")
    telegram = config.get("CONFIG", "TELEGRAM_BOT_TOKEN")

    ls = codeInFutuGroup(group,host,port)
    code_list = ls['code'].tolist()

    quote_ctx = ft.OpenQuoteContext(host=host, port=port)
    handler = CurKlineCallback(callback=create_callback(config), seconds=get_kline_seconds(type))
    quote_ctx.set_handler(handler)  # 设置实时K线回调
    # "K_1M,K_5M,K_15M,K_30M,K_60M,K_DAY,K_WEEK,K_MON" 订阅 K 线数据类型，OpenD 开始持续收到服务器的推送 
    ret, data = quote_ctx.subscribe(code_list, type)
    if ret == ft.RET_OK:
        print(data)
    else:
        print('error:', data)
    while True:
        time.sleep(60)  # 每分钟检查一次是否需要退出
        print('------{}------\n'.format(datetime.datetime.now().strftime('%Y-%m-%d-%H:%M:%S')))
    quote_ctx.unsubscribe(code_list, type)
    quote_ctx.close()   # 关闭当条连接，OpenD 会在1分钟后自动取消相应股票相应类型的订阅    