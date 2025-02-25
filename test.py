from futu import *
from datetime import datetime
import pandas as pd

# 定义参数
stock_code = 'SH.000922'
start_date = '2016-01-01'
end_date = datetime.now().strftime('%Y-%m-%d')

# 计算工作日数量
start = pd.to_datetime(start_date)
end = pd.to_datetime(end_date)
bdays = len(pd.date_range(start=start, end=end, freq='B'))

# 创建行情对象
quote_ctx = OpenQuoteContext(host='127.0.0.1', port=11111)

try:
    # 获取K线数据
    kline_data = quote_ctx.request_history_kline(
        stock_code,
        start=start_date,
        end=end_date,
        max_count=bdays  # 使用计算出的工作日数量
    )
    
    if kline_data[0] == RET_OK:
        # 转换为DataFrame
        df = pd.DataFrame(kline_data[1])
        
        # 重新组织数据结构
        # 计算pre_close（前一日收盘价）
        df['pre_close'] = df['close'].shift(1)
        
        # 选择并重排列顺序
        df = df[['time_key', 'close', 'pre_close', 'high', 'low', 'open', 'volume']]
        
        # 重命名time_key列为第一列（不带列名）
        df = df.rename(columns={'time_key': ''})
        
        # 生成CSV文件名
        clean_code = stock_code.replace('.', '_')
        csv_filename = f'data_{clean_code}_{start_date}_{end_date}.csv'
        
        # 保存到CSV
        df.to_csv(csv_filename, index=False)
        print(f'数据已保存到: {csv_filename}')
    else:
        print('获取历史K线数据失败')

finally:
    # 确保关闭连接
    quote_ctx.close()