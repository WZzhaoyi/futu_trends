from utility import get_data, TOSQL, create_engine
from ft_config import get_config
import pandas as pd
import glob
import os

# 获取配置
config = get_config()
ticker_list_path = config.get("CONFIG", "TICKER_LIST_PATH")
database_path = config.get("CONFIG", "TICKER_DATA_PATH")  # 这里可以配置完整路径，如 /path/to/database/stocks.db

# 获取最新的 CSV 文件
list_of_files = glob.glob(os.path.join(ticker_list_path, "stock_piotroski_list_*.csv"))
latest_file = max(list_of_files, key=os.path.getctime)

# 读取 CSV 文件
df = pd.read_csv(latest_file)
target_list = df['Ticker'].tolist()

# 获取数据并存入数据库
Data = get_data(target_list)
DataEngine = create_engine(database_path)
TOSQL(Data, DataEngine)
