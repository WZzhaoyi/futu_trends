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
#  Written by Joey <wzzhaoyi@outlook.com>, 2023
#  Copyright (c)  Joey - All Rights Reserved

from futu import *
from telegram_bot import TelegramBotEngine

def divide():
    upper_limit = 1.1
    average = 0.9
    lower_limit = 0.8

    quote_ctx = OpenQuoteContext(host='127.0.0.1', port=11111)  # 创建行情对象
    dividend = quote_ctx.get_market_snapshot('SH.000922') # 获取中证红利 的快照数据
    divisor = quote_ctx.get_market_snapshot('SH.000985') # 获取中证全指 的快照数据
    engine = TelegramBotEngine()
    precent = round((dividend[1]['last_price'][0])/(divisor[1]['last_price'][0]), 1)
    content = '比值{} 中证红利{} 中证全指{}'.format(precent, dividend, divisor)

    if precent < average:
        engine.send_telegram_message('中证红利/中证全指 较小：{} (阈值{})'.format(content, average), 'https://xueqiu.com/S/SH000922')
    else:
        engine.send_telegram_message('中证红利/中证全指 较大：{} (阈值{})'.format(precent, average), 'https://xueqiu.com/S/SH000922')

    quote_ctx.close() # 关闭对象，防止连接条数用尽

if __name__ == "__main__":
    divide()