from datetime import datetime
from pathlib import Path
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# --- 1. 设置数据库路径 (必须在导入引擎前完成) ---
from vnpy.event import Event, EventEngine
from vnpy.trader.engine import LogData, MainEngine
from vnpy.trader.event import EVENT_LOG
from vnpy.trader.setting import SETTINGS
from vnpy_portfoliostrategy.backtesting import BacktestingEngine


# --- 2. 导入所需模块 ---
# 从回测研究模块导入回测引擎
from vnpy_ctabacktester import BacktesterEngine
# 从我们本地的策略文件导入策略类
from momentum_rotation_strategy import MomentumRotationStrategy
from vnpy.trader.constant import Interval


# --- 3. 定义回测参数 ---
# 合约和交易所设置
VT_SYMBOL = ["QQQ.SMART","GLD.SMART","SPY.SMART","FXI.SMART"]  # 用来驱动回测时间的主合约
INTERVAL = Interval.DAILY # 时间周期

# 回测时间范围
START_DATE = "2014-09-09"
END_DATE = "2025-09-09"

# 交易成本设置
RATE = 0.001       # 手续费率 (0.1%)
SLIPPAGE = 0.01    # 滑点 (1美分)
CAPITAL = 1000000 # 初始资金
SIZE = 1
PRICETICK = 0.001


# 策略的内部参数
STRATEGY_PARAMS = {
    "regression_window": 22,
    "fixed_capital": CAPITAL,
    "empty_symbol": "",
    "daily_stop_ratio": 0
}

# --- 5. 编写主执行函数 ---
def run_backtest():
    """
    主函数：配置并运行回测
    """
    print("初始化回测引擎...")
    # main_engine = MainEngine()
    # event_engine = EventEngine()
    engine = BacktestingEngine()
    
    print("配置引擎参数...")
    engine.set_parameters(
        vt_symbols=VT_SYMBOL,
        interval=INTERVAL,
        start=datetime.strptime(START_DATE, "%Y-%m-%d"),
        end=datetime.strptime(END_DATE, "%Y-%m-%d"),
        rates={symbol: RATE for symbol in VT_SYMBOL},
        slippages={symbol: SLIPPAGE for symbol in VT_SYMBOL},
        capital=CAPITAL,
        sizes={symbol: SIZE for symbol in VT_SYMBOL},
        priceticks={symbol: PRICETICK for symbol in VT_SYMBOL}
    )
    
    print("添加策略到引擎...")
    engine.add_strategy(MomentumRotationStrategy, STRATEGY_PARAMS)
    
    print("加载历史数据...")
    engine.load_data()
    
    print("开始运行回测...")
    engine.run_backtesting()
    
    print("计算策略绩效...")
    daily_df = engine.calculate_result()
    statistics = engine.calculate_statistics(daily_df)
    


# --- 5. 脚本入口 ---
if __name__ == "__main__":
    run_backtest()