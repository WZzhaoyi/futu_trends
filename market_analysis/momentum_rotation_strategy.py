from datetime import timedelta
import datetime
import numpy as np
import math
from typing import List, Dict

from vnpy.trader.database import get_database
from vnpy_portfoliostrategy import StrategyTemplate as PortfolioTemplate
from vnpy.trader.object import BarData, TradeData
from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.utility import ArrayManager

class MomentumRotationStrategy(PortfolioTemplate):
    """
    基于动量评分的多ETF轮动策略
    - 评分公式: 年化收益率 * R-squared
    - 每日调仓，始终持有评分最高的ETF
    """

    # --- 策略参数 ---
    regression_window: int = 24         # 线性回归窗口
    fixed_capital: int = 1_000_000      # 初始市值
    empty_symbol: str = ""              # 空仓合约代码
    daily_stop_ratio: float = 0         # 日内涨幅止盈比例

    holding_symbol: str = ""            # 持仓合约代码
    holding_volume: int = 0             # 持仓数量
    cash: int = 0                       # 现金
    
    parameters = [
        "regression_window",
        "fixed_capital",
        "empty_symbol",
        "daily_stop_ratio"
    ]

    variables = [
        "holding_symbol",
        "holding_volume",
        "cash"
    ]
    

    def __init__(self, strategy_engine, strategy_name, vt_symbols, setting):
        super().__init__(strategy_engine=strategy_engine,
        strategy_name=strategy_name,
        vt_symbols=vt_symbols,
        setting=setting)
        self.fixed_capital = setting.get("fixed_capital", 1_000_000)
        self.empty_symbol = setting.get("empty_symbol", "")
        self.regression_window = setting.get("regression_window", 24)
        self.daily_stop_ratio = setting.get("daily_stop_ratio", 0)
        self.cash = self.fixed_capital

    def write_log(self, msg: str, source: str = ""):
        print(msg)

    def on_init(self):
        """策略初始化"""
        size: int = self.regression_window + 10

        # 创建每个合约的时序数据容器
        self.ams: dict[str, ArrayManager] = {}

        for vt_symbol in self.vt_symbols:
            self.ams[vt_symbol] = ArrayManager(size)

        self.write_log("策略初始化")

    def on_start(self):
        """策略启动"""
        self.write_log("策略启动")

    def on_stop(self):
        """策略停止"""
        self.write_log("策略停止")

    def on_bars(self, bars: dict[str, BarData]):
        """每个K线收盘时触发"""
        for vt_symbol, bar in bars.items():
            am: ArrayManager = self.ams[vt_symbol]
            am.update_bar(bar)

        # 计算每只ETF的分数
        score_data: dict[str, float] = {}

        for vt_symbol, bar in bars.items():
            am: ArrayManager = self.ams[vt_symbol]
            if not am.inited:
                return

            data: np.array = am.close[-self.regression_window:]
            score_data[vt_symbol] = self.calculate_momentum_score(data)
        
        # self.set_target(self.holding_symbol, 0)
        # 选出得分领先的ETF
        target_symbol: str = max(score_data, key=score_data.get)
        
        # 日内止盈
        if self.holding_symbol == target_symbol and self.daily_stop_ratio > 0:
            am: ArrayManager = self.ams[self.holding_symbol]
            if not am.inited or len(am.close) < 2:
                return
            today_close_price = am.close[-1]
            last_close_price = am.close[-2]
            daily_return = (today_close_price / last_close_price) - 1
            if daily_return > self.daily_stop_ratio:
                self.holding_volume = int(self.holding_volume / 3)
                self.set_target(self.holding_symbol, self.holding_volume)
                self.rebalance_portfolio(bars)
                self.write_log(f"日内止盈，{self.holding_symbol} 止盈 {daily_return}")
                self.put_event()
                return
        
        # 合约切换
        if target_symbol != self.holding_symbol:
            cash = self.holding_volume * bars[self.holding_symbol].close_price + self.cash if self.holding_symbol else self.cash
            
            # print(self.get_pos(self.holding_symbol))
            # 重置合约
            self.set_target(self.holding_symbol, 0)
            # print(self.holding_symbol, target_symbol)

            self.holding_symbol: str = target_symbol
            # 特定合约执行空仓
            if self.holding_symbol == self.empty_symbol:
                self.cash += cash
                self.holding_volume = 0
                self.rebalance_portfolio(bars)
                self.put_event()
                return

            price: float = bars[self.holding_symbol].close_price

            # 交易数量
            volume: int = int((cash / price))
            # print(volume, price, cash)
            self.holding_volume = volume
            self.set_target(self.holding_symbol, volume)
            self.cash = cash - volume * price

            # 根据设置好的目标仓位进行交易
            self.rebalance_portfolio(bars)

        # 推送UI更新
        self.put_event()

    def calculate_momentum_score(self, data: np.array) -> float:
        """计算强弱得分"""
        # 确保数据中没有0或负数
        x = np.arange(len(data))
        y = np.log(data)

        slope, intercept = np.polyfit(x, y, 1)
        annualized_returns = math.pow(math.exp(slope), 250) - 1
        
        # 计算R-squared
        y_pred = slope * x + intercept
        ss_res = np.sum((y - y_pred) ** 2)
        ss_tot = np.sum((y - np.mean(y)) ** 2)
        r_squared = 1 - (ss_res / ss_tot) if ss_tot != 0 else 0
        
        score = annualized_returns * r_squared
        return score