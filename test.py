from futu import *

quote_ctx = OpenQuoteContext(host='127.0.0.1', port=11111)  # 创建行情对象
snapshot = quote_ctx.get_market_snapshot('SH.000922') # 获取港股 HK.00700 的快照数据
print(snapshot[1]['last_price'])

kline = quote_ctx.request_history_kline('SH.000922', max_count = 120, )
print(kline[1]['close'])

quote_ctx.close() # 关闭对象，防止连接条数用尽