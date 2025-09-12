import yfinance as yf
import pandas as pd
from pathlib import Path
from datetime import datetime

# 1. 动态设置VNpy的数据库路径
# 这一步是关键，必须在导入任何VNpy核心模块（如database_manager）之前执行
from vnpy.trader.setting import SETTINGS

# 获取当前脚本文件所在的目录
# Path(__file__) 获取当前文件的绝对路径
# .parent 获取该路径的父目录
# PROJECT_ROOT = Path(__file__).parent 
PROJECT_DATA_ROOT = Path('./data')
DB_DIR = PROJECT_DATA_ROOT.joinpath("vnpy_data")
DB_DIR.mkdir(exist_ok=True) # 如果database文件夹不存在，则创建

# 2. 导入VNpy相关模块（在设置路径之后）
from vnpy.trader.database import get_database
from vnpy.trader.object import BarData
from vnpy.trader.constant import Exchange, Interval

# 3. 定义要处理的ETF列表和数据参数
SYMBOLS = ["SPY", "GLD", "QQQ", "CNXT", "FXI"]
EXCHANGE = Exchange.SMART
START_DATE = "2014-09-09"
END_DATE = "2025-09-09"

def download_and_import_data(symbols:list[str],start_date:str,end_date:str,exchange:Exchange):
    """
    使用yfinance下载数据并导入到指定的VNpy数据库中。
    """

    for symbol in symbols:
        print(f"开始处理和导入 {symbol} 的数据...")
        csv_file_path = DB_DIR.joinpath(f'{symbol} {start_date} {end_date}.csv')
        if csv_file_path.exists():
            print(f"从本地文件读取 {symbol} 的数据...")
            df = pd.read_csv(csv_file_path, index_col=0, parse_dates=True)
        else:
            print(f"从yfinance下载 {symbol} 的数据...")
            df = yf.Ticker(symbol).history(start=start_date, end=end_date, interval="1d", adjust_price=True)
            df.dropna(inplace=True) # 去除数据中的NaN值
            df.to_csv(csv_file_path)
        
        bars = []
        for ix, row in df.iterrows():
            # yfinance返回的index是Timestamp类型，可以直接使用
            bar = BarData(
                symbol=symbol,
                exchange=exchange,  # 通用交易所
                datetime=ix.to_pydatetime(), # 转换为Python原生的datetime对象
                interval=Interval.DAILY,
                volume=row["Volume"],
                open_price=row["Open"],
                high_price=row["High"],
                low_price=row["Low"],
                close_price=row["Close"],
                gateway_name="DB", # 表明这是数据库来的数据
            )
            bars.append(bar)
        
        get_database().save_bar_data(bars)
        print(f"成功将 {len(bars)} 条 {symbol} 的历史数据导入数据库。")

if __name__ == "__main__":
    download_and_import_data(SYMBOLS,START_DATE,END_DATE,EXCHANGE)